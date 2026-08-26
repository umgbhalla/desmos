//! stdin markdown → stdout HTML. Same `html()` Desk POST /md runs.

fn main() {
    let mut src = String::new();
    std::io::Read::read_to_string(&mut std::io::stdin(), &mut src).expect("read stdin");
    print!("{}", desmos_md_html::html(&src));
}
