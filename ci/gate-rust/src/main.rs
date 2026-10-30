//! gate-rust: what gate.py's Rust checks need to know of a Rust source file, read with syn.
//!
//! `gate-rust FILE...` reads each FILE, every argument a path, and prints one line of JSON per file, in the order
//! given: the file's functions with their lines, attributes, assertion macros, discarded values and the flow of their
//! bodies, its inline modules, and every name it binds; or, for a file it cannot read, decode or parse, an error
//! document saying why. It exits 0 once every line is written, and 1, saying so on standard error, when standard
//! output cannot be written to. The schema is written down once, in docs/quality-gate.md ("Reading Rust: the
//! gate-rust helper"); `SCHEMA` is its version, which gate.py checks, and a change to what this prints raises both.

use std::ffi::OsString;
use std::io::{self, BufWriter, Write};
use std::process::ExitCode;

use proc_macro2::{Group, Ident, Punct, Spacing, Span, TokenStream, TokenTree};
use serde_json::{json, Value};
use syn::ext::IdentExt;
use syn::punctuated::Punctuated;
use syn::spanned::Spanned;
use syn::visit::{self, Visit};
use syn::{AttrStyle, Attribute, BinOp, Block, Expr, FnArg, Meta, Pat, Signature, Stmt, Token, Visibility};

/// The version of the schema this prints, its documents' `schema` field.
const SCHEMA: u64 = 2;

/// The macros a test asserts with, by the last segment of their path.
const ASSERTION_MACROS: &[&str] = &[
    "assert",
    "assert_eq",
    "assert_ne",
    "debug_assert",
    "debug_assert_eq",
    "debug_assert_ne",
    "panic",
];

/// The identifier `shape` writes after a `$` in place of each literal of an assertion's arguments.
const LITERAL_STANDIN: &str = "lit";

fn main() -> ExitCode {
    let paths: Vec<OsString> = std::env::args_os().skip(1).collect();
    let mut out = BufWriter::new(io::stdout().lock());
    for path in &paths {
        let line = document(std::fs::read(path)).to_string();
        if let Err(e) = writeln!(out, "{line}") {
            eprintln!("gate-rust: cannot write to standard output: {e}");
            return ExitCode::FAILURE;
        }
    }
    if let Err(e) = out.flush() {
        eprintln!("gate-rust: cannot write to standard output: {e}");
        return ExitCode::FAILURE;
    }
    ExitCode::SUCCESS
}

/// The document, as the schema has it, of a file whose contents reading it gave: an error document when it could
/// not be read, is not UTF-8 or does not parse.
fn document(read: io::Result<Vec<u8>>) -> Value {
    let bytes = match read {
        Ok(bytes) => bytes,
        Err(e) => return error_document(&format!("could not be read: {e}"), None),
    };
    let source = match String::from_utf8(bytes) {
        Ok(source) => source,
        Err(e) => return error_document(&format!("is not UTF-8: {e}"), None),
    };
    match syn::parse_file(&source) {
        Ok(file) => {
            let mut collector = Collector::default();
            collector.visit_file(&file);
            json!({
                "schema": SCHEMA,
                "fns": collector.fns.iter().map(FnRecord::to_json).collect::<Vec<_>>(),
                "mods": collector.mods,
                "bindings": collector.bindings,
            })
        }
        Err(e) => {
            // syn gives an error at the end of the input the call site's span, which holds no source and which
            // proc-macro2 places on line 1.
            let span = e.span();
            error_document(&e.to_string(), span.source_text().map(|_| span.start().line))
        }
    }
}

fn error_document(message: &str, line: Option<usize>) -> Value {
    json!({"schema": SCHEMA, "error": {"message": message, "line": line}})
}

/// One of the schema's `fns`, while its body is being walked.
struct FnRecord {
    name: String,
    line: usize,
    attrs_line: usize,
    end: usize,
    holders: Vec<Value>,
    attrs: Vec<Value>,
    asserts: Vec<Value>,
    discards: Vec<Value>,
    body_digest: Option<String>,
    flow: Option<Vec<Value>>,
}

impl FnRecord {
    fn to_json(&self) -> Value {
        json!({
            "name": self.name,
            "line": self.line,
            "attrs_line": self.attrs_line,
            "end": self.end,
            "holders": self.holders,
            "attrs": self.attrs,
            "asserts": self.asserts,
            "discards": self.discards,
            "body_digest": self.body_digest,
            "flow": self.flow,
        })
    }
}

/// Walks a parsed file and gathers the schema's `fns`, `mods` and `bindings`.
#[derive(Default)]
struct Collector {
    fns: Vec<FnRecord>,
    mods: Vec<Value>,
    bindings: Vec<Value>,
    /// The items the walk is inside, outermost first, as a `fn` met there lists them in `holders`.
    holders: Vec<Value>,
    /// The index in `fns` of the innermost `fn` the walk is inside, which owns the assertions and discards met;
    /// `None` outside every `fn`, where they belong to none.
    current_fn: Option<usize>,
    /// The binding kind, `if-let` or `while-let`, of a `let` met while the walk is inside an `if`'s or a `while`'s
    /// condition; `None` elsewhere, where such a `let` counts as `if-let`.
    condition_kind: Option<&'static str>,
}

impl Collector {
    fn bind(&mut self, ident: &Ident, kind: &str) {
        let name = ident.unraw().to_string();
        if name != "_" {
            self.bindings.push(json!({"name": name, "kind": kind, "line": ident.span().start().line}));
        }
    }

    fn bind_pattern(&mut self, pat: &Pat, kind: &str) {
        for ident in pattern_names(pat) {
            self.bind(ident, kind);
        }
    }

    /// Runs `walk` with `holder`, a holder as the schema has one, innermost among the holders.
    fn inside_holder(&mut self, holder: Value, walk: impl FnOnce(&mut Self)) {
        self.holders.push(holder);
        walk(self);
        self.holders.pop();
    }

    /// Records a `fn`, ending on line `end`, and its name and parameters as bindings, then runs `walk`, syn's walk of
    /// it, with the `fn` the innermost holder and the owner of the assertions and discards met. `vis` is `None` for a
    /// trait's `fn`, which has none.
    fn function(
        &mut self,
        attrs: &[Attribute],
        vis: Option<&Visibility>,
        sig: &Signature,
        body: Option<&Block>,
        end: usize,
        walk: impl FnOnce(&mut Self),
    ) {
        let line = match vis {
            Some(Visibility::Inherited) | None => sig.span().start().line,
            Some(vis) => vis.span().start().line,
        };
        let name = sig.ident.unraw().to_string();
        self.fns.push(FnRecord {
            name: name.clone(),
            line,
            attrs_line: attrs_line(attrs, line),
            end,
            holders: self.holders.clone(),
            attrs: listed_attrs(attrs),
            asserts: Vec::new(),
            discards: Vec::new(),
            body_digest: body.and_then(|b| tokens_text(b.span())).map(|t| digest(&t)),
            // A fn another fn holds is in the flow of its holder's body, as a `fn` node.
            flow: body.filter(|_| !self.holders.iter().any(|h| h["kind"] == "fn")).map(flow),
        });
        self.bind(&sig.ident, "fn");
        for input in &sig.inputs {
            if let FnArg::Typed(typed) = input {
                self.bind_pattern(&typed.pat, "param");
            }
        }
        let outer = self.current_fn.replace(self.fns.len() - 1);
        self.inside_holder(json!({"kind": "fn", "name": name}), walk);
        self.current_fn = outer;
    }

    /// Runs `walk`, a walk of a condition, with `kind` as the binding kind of a `let` in it.
    fn in_condition(&mut self, kind: &'static str, walk: impl FnOnce(&mut Self)) {
        let outer = self.condition_kind.replace(kind);
        walk(self);
        self.condition_kind = outer;
    }

    /// Runs `walk` outside any condition, as for the body of an `if` or a `while`, whose `let`s are not the
    /// condition's.
    fn outside_condition(&mut self, walk: impl FnOnce(&mut Self)) {
        let outer = self.condition_kind.take();
        walk(self);
        self.condition_kind = outer;
    }

    fn discard(&mut self, kind: &str, thrown: &Expr) {
        if let Some(i) = self.current_fn {
            self.fns[i].discards.push(json!({
                "kind": kind,
                "line": thrown.span().start().line,
                "text": thrown.span().source_text().unwrap_or_default(),
            }));
        }
    }
}

impl<'ast> Visit<'ast> for Collector {
    fn visit_item_fn(&mut self, i: &'ast syn::ItemFn) {
        let end = i.block.span().end().line;
        self.function(&i.attrs, Some(&i.vis), &i.sig, Some(&i.block), end, |c| visit::visit_item_fn(c, i));
    }

    fn visit_impl_item_fn(&mut self, i: &'ast syn::ImplItemFn) {
        let end = i.block.span().end().line;
        self.function(&i.attrs, Some(&i.vis), &i.sig, Some(&i.block), end, |c| visit::visit_impl_item_fn(c, i));
    }

    fn visit_trait_item_fn(&mut self, i: &'ast syn::TraitItemFn) {
        let end = match (&i.default, &i.semi_token) {
            (Some(body), _) => body.span().end().line,
            (None, Some(semi)) => semi.span.end().line,
            (None, None) => i.sig.span().end().line,
        };
        self.function(&i.attrs, None, &i.sig, i.default.as_ref(), end, |c| visit::visit_trait_item_fn(c, i));
    }

    // What an extern block declares is another language's, and none of it is listed.
    fn visit_item_foreign_mod(&mut self, _: &'ast syn::ItemForeignMod) {}

    fn visit_item_mod(&mut self, i: &'ast syn::ItemMod) {
        let Some((brace, _)) = &i.content else {
            return visit::visit_item_mod(self, i);
        };
        let line = match &i.vis {
            Visibility::Inherited => i.mod_token.span.start().line,
            vis => vis.span().start().line,
        };
        let name = i.ident.unraw().to_string();
        self.mods.push(json!({
            "name": name,
            "line": line,
            "attrs_line": attrs_line(&i.attrs, line),
            "end": brace.span.close().end().line,
            "cfg_test": i.attrs.iter().any(holds_only_under_test),
        }));
        self.inside_holder(json!({"kind": "mod", "name": name}), |c| visit::visit_item_mod(c, i));
    }

    fn visit_item_impl(&mut self, i: &'ast syn::ItemImpl) {
        let holder = json!({
            "kind": "impl",
            "name": tokens_text(i.self_ty.span()),
            "trait": i.trait_.as_ref().map(|(_, path, _)| tokens_text(path.span())),
        });
        self.inside_holder(holder, |c| visit::visit_item_impl(c, i));
    }

    fn visit_item_trait(&mut self, i: &'ast syn::ItemTrait) {
        self.bind(&i.ident, "trait");
        let holder = json!({"kind": "trait", "name": i.ident.unraw().to_string()});
        self.inside_holder(holder, |c| visit::visit_item_trait(c, i));
    }

    fn visit_item_const(&mut self, i: &'ast syn::ItemConst) {
        self.bind(&i.ident, "const");
        let holder = json!({"kind": "const", "name": i.ident.unraw().to_string()});
        self.inside_holder(holder, |c| visit::visit_item_const(c, i));
    }

    fn visit_item_static(&mut self, i: &'ast syn::ItemStatic) {
        self.bind(&i.ident, "static");
        let holder = json!({"kind": "static", "name": i.ident.unraw().to_string()});
        self.inside_holder(holder, |c| visit::visit_item_static(c, i));
    }

    fn visit_item_struct(&mut self, i: &'ast syn::ItemStruct) {
        self.bind(&i.ident, "struct");
        visit::visit_item_struct(self, i);
    }

    fn visit_item_enum(&mut self, i: &'ast syn::ItemEnum) {
        self.bind(&i.ident, "enum");
        visit::visit_item_enum(self, i);
    }

    fn visit_item_union(&mut self, i: &'ast syn::ItemUnion) {
        self.bind(&i.ident, "union");
        visit::visit_item_union(self, i);
    }

    fn visit_item_type(&mut self, i: &'ast syn::ItemType) {
        self.bind(&i.ident, "type");
        visit::visit_item_type(self, i);
    }

    fn visit_impl_item_const(&mut self, i: &'ast syn::ImplItemConst) {
        self.bind(&i.ident, "const");
        visit::visit_impl_item_const(self, i);
    }

    fn visit_impl_item_type(&mut self, i: &'ast syn::ImplItemType) {
        self.bind(&i.ident, "type");
        visit::visit_impl_item_type(self, i);
    }

    fn visit_trait_item_const(&mut self, i: &'ast syn::TraitItemConst) {
        self.bind(&i.ident, "const");
        visit::visit_trait_item_const(self, i);
    }

    fn visit_trait_item_type(&mut self, i: &'ast syn::TraitItemType) {
        self.bind(&i.ident, "type");
        visit::visit_trait_item_type(self, i);
    }

    fn visit_use_rename(&mut self, i: &'ast syn::UseRename) {
        self.bind(&i.rename, "use-as");
        visit::visit_use_rename(self, i);
    }

    fn visit_local(&mut self, i: &'ast syn::Local) {
        self.bind_pattern(&i.pat, "let");
        if let Some(init) = &i.init {
            if is_wild(&i.pat) {
                self.discard("let", &init.expr);
            }
        }
        visit::visit_local(self, i);
    }

    fn visit_stmt(&mut self, i: &'ast Stmt) {
        if let Stmt::Expr(expr, Some(_)) = i {
            match expr {
                Expr::Assign(assign) if matches!(*assign.left, Expr::Infer(_)) => self.discard("assign", &assign.right),
                Expr::MethodCall(call) if call.method == "ok" && call.args.is_empty() => {
                    self.discard("ok", &call.receiver);
                }
                _ => {}
            }
        }
        visit::visit_stmt(self, i);
    }

    fn visit_expr_for_loop(&mut self, i: &'ast syn::ExprForLoop) {
        self.bind_pattern(&i.pat, "for");
        visit::visit_expr_for_loop(self, i);
    }

    fn visit_arm(&mut self, i: &'ast syn::Arm) {
        self.bind_pattern(&i.pat, "match");
        visit::visit_arm(self, i);
    }

    fn visit_expr_closure(&mut self, i: &'ast syn::ExprClosure) {
        for input in &i.inputs {
            self.bind_pattern(input, "closure-param");
        }
        visit::visit_expr_closure(self, i);
    }

    fn visit_expr_let(&mut self, i: &'ast syn::ExprLet) {
        self.bind_pattern(&i.pat, self.condition_kind.unwrap_or("if-let"));
        // What the let matches against, as `{ let y = f(); y }`, is not the condition itself.
        self.outside_condition(|c| visit::visit_expr_let(c, i));
    }

    fn visit_expr_if(&mut self, i: &'ast syn::ExprIf) {
        self.in_condition("if-let", |c| c.visit_expr(&i.cond));
        self.outside_condition(|c| {
            c.visit_block(&i.then_branch);
            if let Some((_, otherwise)) = &i.else_branch {
                c.visit_expr(otherwise);
            }
        });
    }

    fn visit_expr_while(&mut self, i: &'ast syn::ExprWhile) {
        self.in_condition("while-let", |c| c.visit_expr(&i.cond));
        self.outside_condition(|c| c.visit_block(&i.body));
    }

    fn visit_macro(&mut self, i: &'ast syn::Macro) {
        let name = i.path.segments.last().map(|s| s.ident.unraw().to_string());
        if let (Some(owner), Some(name)) = (self.current_fn, name) {
            if ASSERTION_MACROS.contains(&name.as_str()) {
                let mut literals = Vec::new();
                let shape = blank_literals(i.tokens.clone(), &mut literals).to_string();
                self.fns[owner].asserts.push(json!({
                    "name": name,
                    "line": i.path.span().start().line,
                    "text": i.span().source_text().unwrap_or_default(),
                    "shape": shape,
                    "literals": literals,
                }));
            }
        }
        // Arguments that parse as expressions separated by commas, such as an assertion's or vec![...]'s, are walked
        // as code; any other macro's tokens are a language of its own.
        if let Ok(args) = i.parse_body_with(Punctuated::<Expr, Token![,]>::parse_terminated) {
            for arg in &args {
                self.visit_expr(arg);
            }
        }
        visit::visit_macro(self, i);
    }
}

/// The schema's `flow` of a `fn` whose body is `body`.
fn flow(body: &Block) -> Vec<Value> {
    let mut walker = FlowWalker::default();
    walker.visit_block(body);
    walker.nodes
}

/// Walks a `fn`'s body and gathers the schema's `flow` nodes of it.
#[derive(Default)]
struct FlowWalker {
    /// The nodes of the list the walk is filling, which a node met goes at the end of.
    nodes: Vec<Value>,
}

impl FlowWalker {
    /// The nodes `walk`, a walk of one part of a node, meets, as the list of that part.
    fn part(&mut self, walk: impl FnOnce(&mut Self)) -> Vec<Value> {
        let outer = std::mem::take(&mut self.nodes);
        walk(self);
        std::mem::replace(&mut self.nodes, outer)
    }

    fn push(&mut self, kind: &str, at: Span, mut parts: Value) {
        parts["kind"] = json!(kind);
        parts["line"] = json!(at.start().line);
        self.nodes.push(parts);
    }

    /// Adds a `fn` node for a `fn` the body holds, `method` saying whether it is an `impl`'s or a `trait`'s.
    fn nested_fn(&mut self, sig: &Signature, body: &Block, method: bool) {
        let nodes = self.part(|w| w.visit_block(body));
        let name = sig.ident.unraw().to_string();
        self.push("fn", sig.fn_token.span, json!({"name": name, "method": method, "body": nodes}));
    }

    /// Adds a `break` or `continue` node, `labelled` saying whether it names a label.
    fn jump(&mut self, kind: &str, at: Span, labelled: bool) {
        self.push(kind, at, json!({"labelled": labelled}));
    }
}

impl<'ast> Visit<'ast> for FlowWalker {
    fn visit_expr_if(&mut self, i: &'ast syn::ExprIf) {
        let cond = self.part(|w| w.visit_expr(&i.cond));
        let then = self.part(|w| w.visit_block(&i.then_branch));
        let (otherwise, else_if) = match &i.else_branch {
            Some((_, branch)) => (Some(self.part(|w| w.visit_expr(branch))), matches!(**branch, Expr::If(_))),
            None => (None, false),
        };
        self.push("if", i.if_token.span, json!({"cond": cond, "then": then, "else": otherwise, "else_if": else_if}));
    }

    fn visit_local(&mut self, i: &'ast syn::Local) {
        let Some(init) = &i.init else {
            return visit::visit_local(self, i);
        };
        self.visit_expr(&init.expr);
        if let Some((else_token, diverge)) = &init.diverge {
            let otherwise = self.part(|w| w.visit_expr(diverge));
            self.push("let-else", else_token.span, json!({"else": otherwise}));
        }
    }

    fn visit_expr_match(&mut self, i: &'ast syn::ExprMatch) {
        let scrutinee = self.part(|w| w.visit_expr(&i.expr));
        let arms = self.part(|w| {
            for arm in &i.arms {
                if let Some((_, guard)) = &arm.guard {
                    w.visit_expr(guard);
                }
                w.visit_expr(&arm.body);
            }
        });
        self.push("match", i.match_token.span, json!({"scrutinee": scrutinee, "arms": arms}));
    }

    fn visit_expr_loop(&mut self, i: &'ast syn::ExprLoop) {
        let body = self.part(|w| w.visit_block(&i.body));
        self.push("loop", i.loop_token.span, json!({"head": [], "body": body}));
    }

    fn visit_expr_while(&mut self, i: &'ast syn::ExprWhile) {
        let head = self.part(|w| w.visit_expr(&i.cond));
        let body = self.part(|w| w.visit_block(&i.body));
        self.push("while", i.while_token.span, json!({"head": head, "body": body}));
    }

    fn visit_expr_for_loop(&mut self, i: &'ast syn::ExprForLoop) {
        let head = self.part(|w| w.visit_expr(&i.expr));
        let body = self.part(|w| w.visit_block(&i.body));
        self.push("for", i.for_token.span, json!({"head": head, "body": body}));
    }

    fn visit_expr_break(&mut self, i: &'ast syn::ExprBreak) {
        self.jump("break", i.break_token.span, i.label.is_some());
        if let Some(value) = &i.expr {
            self.visit_expr(value);
        }
    }

    fn visit_expr_continue(&mut self, i: &'ast syn::ExprContinue) {
        self.jump("continue", i.continue_token.span, i.label.is_some());
    }

    fn visit_expr_binary(&mut self, i: &'ast syn::ExprBinary) {
        if !is_logical(&i.op) {
            return visit::visit_expr_binary(self, i);
        }
        let mut ops = Vec::new();
        let mut operands = Vec::new();
        logical_run(i, &mut ops, &mut operands);
        let nodes = self.part(|w| operands.iter().for_each(|o| w.visit_expr(o)));
        self.push("bool", i.span(), json!({"ops": ops, "operands": nodes}));
    }

    fn visit_expr_closure(&mut self, i: &'ast syn::ExprClosure) {
        let body = self.part(|w| w.visit_expr(&i.body));
        self.push("closure", i.or1_token.span, json!({"body": body}));
    }

    fn visit_item_fn(&mut self, i: &'ast syn::ItemFn) {
        self.nested_fn(&i.sig, &i.block, false);
    }

    fn visit_impl_item_fn(&mut self, i: &'ast syn::ImplItemFn) {
        self.nested_fn(&i.sig, &i.block, true);
    }

    fn visit_trait_item_fn(&mut self, i: &'ast syn::TraitItemFn) {
        if let Some(body) = &i.default {
            self.nested_fn(&i.sig, body, true);
        }
    }

    fn visit_expr_call(&mut self, i: &'ast syn::ExprCall) {
        if let Expr::Path(callee) = &*i.func {
            if callee.qself.is_none() {
                let path: Vec<String> = callee.path.segments.iter().map(|s| s.ident.unraw().to_string()).collect();
                self.push("call", callee.span(), json!({"path": path}));
            }
        }
        visit::visit_expr_call(self, i);
    }

    fn visit_expr_method_call(&mut self, i: &'ast syn::ExprMethodCall) {
        if let Expr::Path(receiver) = &*i.receiver {
            if receiver.qself.is_none() && receiver.path.is_ident("self") {
                self.push("call", receiver.span(), json!({"path": ["self", i.method.unraw().to_string()]}));
            }
        }
        visit::visit_expr_method_call(self, i);
    }

    // What an extern block declares has no body.
    fn visit_item_foreign_mod(&mut self, _: &'ast syn::ItemForeignMod) {}

    fn visit_macro(&mut self, i: &'ast syn::Macro) {
        // As Collector reads a macro's arguments.
        if let Ok(args) = i.parse_body_with(Punctuated::<Expr, Token![,]>::parse_terminated) {
            for arg in &args {
                self.visit_expr(arg);
            }
        }
    }
}

/// Whether `op` is `&&` or `||`.
fn is_logical(op: &BinOp) -> bool {
    matches!(op, BinOp::And(_) | BinOp::Or(_))
}

/// Appends to `ops` the operators of the run of `&&` and `||` that `binary`, one of them, starts, through
/// parentheses, in the order they are written, and to `operands` what they join.
fn logical_run<'a>(binary: &'a syn::ExprBinary, ops: &mut Vec<&'static str>, operands: &mut Vec<&'a Expr>) {
    logical_operand(&binary.left, ops, operands);
    ops.push(if matches!(binary.op, BinOp::And(_)) { "&&" } else { "||" });
    logical_operand(&binary.right, ops, operands);
}

/// What `logical_run` does with an operand, `expr`: continues the run with it when it is a `&&` or `||`, through
/// parentheses, and appends it to `operands` otherwise.
fn logical_operand<'a>(expr: &'a Expr, ops: &mut Vec<&'static str>, operands: &mut Vec<&'a Expr>) {
    match expr {
        Expr::Paren(inner) => logical_operand(&inner.expr, ops, operands),
        Expr::Binary(binary) if is_logical(&binary.op) => logical_run(binary, ops, operands),
        _ => operands.push(expr),
    }
}

/// The identifiers `pat` binds, in the order they are written: each identifier pattern in it but those that start
/// with an uppercase letter, which name a constant or a unit variant, such as `None`, rather than bind one.
fn pattern_names(pat: &Pat) -> Vec<&Ident> {
    let mut out = Vec::new();
    // Patterns still to read, the next one last, so that pushing a pattern's parts in reverse reads them in order.
    let mut pending = vec![pat];
    while let Some(pat) = pending.pop() {
        match pat {
            Pat::Ident(p) => {
                if !p.ident.unraw().to_string().starts_with(char::is_uppercase) {
                    out.push(&p.ident);
                }
                if let Some((_, sub)) = &p.subpat {
                    pending.push(sub);
                }
            }
            Pat::Or(p) => pending.extend(p.cases.iter().rev()),
            Pat::Paren(p) => pending.push(&p.pat),
            Pat::Reference(p) => pending.push(&p.pat),
            Pat::Slice(p) => pending.extend(p.elems.iter().rev()),
            Pat::Struct(p) => pending.extend(p.fields.iter().rev().map(|f| &*f.pat)),
            Pat::Tuple(p) => pending.extend(p.elems.iter().rev()),
            Pat::TupleStruct(p) => pending.extend(p.elems.iter().rev()),
            Pat::Type(p) => pending.push(&p.pat),
            _ => {}
        }
    }
    out
}

/// Whether `pat` is `_`, or `_: T`, which binds nothing and drops the value it is given at once.
fn is_wild(pat: &Pat) -> bool {
    match pat {
        Pat::Wild(_) => true,
        Pat::Type(p) => matches!(*p.pat, Pat::Wild(_)),
        _ => false,
    }
}

/// The line the first of an item's outer attributes starts on, doc comments included; `line`, the item's own first
/// line, when it has none.
fn attrs_line(attrs: &[Attribute], line: usize) -> usize {
    attrs
        .iter()
        .filter(|a| matches!(a.style, AttrStyle::Outer))
        .map(|a| a.span().start().line)
        .min()
        .unwrap_or(line)
}

/// The schema's `attrs` of an item whose attributes are `attrs`: the outer ones but `doc`, which doc comments are.
fn listed_attrs(attrs: &[Attribute]) -> Vec<Value> {
    attrs
        .iter()
        .filter(|a| matches!(a.style, AttrStyle::Outer) && !a.path().is_ident("doc"))
        .map(|a| {
            json!({
                "path": a.path().segments.iter().map(|s| s.ident.unraw().to_string()).collect::<Vec<_>>().join("::"),
                "text": a.span().source_text().unwrap_or_default(),
                "line": a.span().start().line,
            })
        })
        .collect()
}

/// Whether `attr` is a `cfg` whose predicate holds only under `cfg(test)`, as the schema's `cfg_test` says.
fn holds_only_under_test(attr: &Attribute) -> bool {
    attr.path().is_ident("cfg") && attr.parse_args::<Meta>().is_ok_and(|p| needs_test(&p))
}

fn needs_test(predicate: &Meta) -> bool {
    match predicate {
        Meta::Path(path) => path.is_ident("test"),
        Meta::List(list) if list.path.is_ident("all") => list
            .parse_args_with(Punctuated::<Meta, Token![,]>::parse_terminated)
            .is_ok_and(|all| all.iter().any(needs_test)),
        _ => false,
    }
}

/// `tokens` with each literal in it, at any depth, replaced by `$` and `LITERAL_STANDIN`, each literal's text
/// appended to `literals` in order.
fn blank_literals(tokens: TokenStream, literals: &mut Vec<String>) -> TokenStream {
    let mut out = TokenStream::new();
    for token in tokens {
        match token {
            TokenTree::Literal(lit) => {
                literals.push(lit.to_string());
                out.extend([
                    TokenTree::Punct(Punct::new('$', Spacing::Joint)),
                    TokenTree::Ident(Ident::new(LITERAL_STANDIN, lit.span())),
                ]);
            }
            TokenTree::Group(group) => {
                let inner = blank_literals(group.stream(), literals);
                out.extend([TokenTree::Group(Group::new(group.delimiter(), inner))]);
            }
            other => out.extend([other]),
        }
    }
    out
}

/// The tokens of the source under `span`, as proc-macro2 writes them, without the source's layout and comments;
/// `None` for a span of no place in the source, which syn gives only to tokens it made up, never to parsed ones.
fn tokens_text(span: Span) -> Option<String> {
    let tokens: TokenStream = span.source_text()?.parse().ok()?;
    Some(tokens.to_string())
}

/// 64-bit FNV-1a of `text`, as 16 hexadecimal digits (http://www.isthe.com/chongo/tech/comp/fnv/).
fn digest(text: &str) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for byte in text.bytes() {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    format!("{hash:016x}")
}
