//! HTML from Grok's markdown parser.
//!
//! Grammar is [`xai_grok_markdown_core::offset_events`]: the same
//! pulldown-cmark options and double-tilde-only strikethrough the TUI uses.
//! Fence colors are syntect Tokyo Night, the same theme the TUI highlighter
//! loads. Keyword sets are not a highlighter.

use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Tag, TagEnd};
use std::io::Cursor;
use std::sync::OnceLock;
use syntect::easy::HighlightLines;
use syntect::highlighting::{Theme, ThemeSet};
use syntect::parsing::SyntaxSet;
use syntect::util::LinesWithEndings;
use xai_grok_markdown_core::offset_events;

struct Walk {
    out: String,
    in_code: bool,
    in_head: bool,
    code_lang: String,
    code_buf: String,
}

pub fn html(src: &str) -> String {
    let mut walk = Walk {
        out: String::with_capacity(src.len().saturating_mul(2).max(32)),
        in_code: false,
        in_head: false,
        code_lang: String::new(),
        code_buf: String::new(),
    };
    for (event, _range) in offset_events(src) {
        if walk.in_code {
            match event {
                Event::Text(text) | Event::Code(text) => walk.code_buf.push_str(&text),
                Event::Html(text) | Event::InlineHtml(text) => walk.code_buf.push_str(&text),
                Event::SoftBreak | Event::HardBreak => walk.code_buf.push('\n'),
                Event::End(TagEnd::CodeBlock) => {
                    flush_fence(&mut walk.out, &walk.code_lang, &walk.code_buf);
                    walk.in_code = false;
                    walk.code_lang.clear();
                    walk.code_buf.clear();
                }
                _ => {}
            }
            continue;
        }
        match event {
            Event::Start(tag) => start_tag(&mut walk, tag),
            Event::End(tag) => end_tag(&mut walk, tag),
            Event::Text(text) => walk.out.push_str(&esc(&text)),
            Event::Code(text) => {
                walk.out.push_str("<code>");
                walk.out.push_str(&esc(&text));
                walk.out.push_str("</code>");
            }
            Event::Html(text) | Event::InlineHtml(text) => walk.out.push_str(&esc(&text)),
            Event::SoftBreak => walk.out.push('\n'),
            Event::HardBreak => walk.out.push_str("<br/>"),
            Event::Rule => walk.out.push_str("<hr/>"),
            Event::TaskListMarker(checked) => {
                walk.out.push_str("<span class=\"box\">");
                if checked {
                    walk.out.push('✓');
                }
                walk.out.push_str("</span>");
            }
            Event::InlineMath(text) => {
                walk.out.push_str("<span class=\"math\">");
                walk.out.push_str(&esc(&text));
                walk.out.push_str("</span>");
            }
            Event::DisplayMath(text) => {
                walk.out.push_str("<div class=\"math display\">");
                walk.out.push_str(&esc(&text));
                walk.out.push_str("</div>");
            }
            Event::FootnoteReference(name) => {
                walk.out.push_str("<sup class=\"fn\">");
                walk.out.push_str(&esc(&name));
                walk.out.push_str("</sup>");
            }
        }
    }
    if walk.in_code {
        flush_fence(&mut walk.out, &walk.code_lang, &walk.code_buf);
    }
    if walk.out.is_empty() {
        walk.out.push_str("<p></p>");
    }
    walk.out
}

fn start_tag(walk: &mut Walk, tag: Tag<'_>) {
    match tag {
        Tag::Paragraph => walk.out.push_str("<p>"),
        Tag::Heading { level, .. } => {
            walk.out.push_str("<h");
            walk.out.push(heading_digit(level));
            walk.out.push('>');
        }
        Tag::BlockQuote(_) => walk.out.push_str("<blockquote>"),
        Tag::CodeBlock(kind) => {
            walk.in_code = true;
            walk.code_lang.clear();
            if let CodeBlockKind::Fenced(info) = kind {
                let lang = info.split_whitespace().next().unwrap_or("");
                walk.code_lang.push_str(lang);
            }
        }
        Tag::List(Some(_)) => walk.out.push_str("<ol>"),
        Tag::List(None) => walk.out.push_str("<ul>"),
        Tag::Item => walk.out.push_str("<li>"),
        Tag::Table(_) => walk.out.push_str("<table>"),
        Tag::TableHead => {
            walk.in_head = true;
            walk.out.push_str("<thead><tr>");
        }
        Tag::TableRow => walk.out.push_str("<tr>"),
        Tag::TableCell => walk.out.push_str(if walk.in_head { "<th>" } else { "<td>" }),
        Tag::Emphasis => walk.out.push_str("<em>"),
        Tag::Strong => walk.out.push_str("<strong>"),
        Tag::Strikethrough => walk.out.push_str("<del>"),
        Tag::Link { dest_url, .. } => {
            walk.out.push_str("<a href=\"");
            walk.out.push_str(&esc(&dest_url));
            walk.out.push_str("\" target=\"_blank\" rel=\"noreferrer\">");
        }
        Tag::Image { dest_url, .. } => {
            walk.out.push_str("<img src=\"");
            walk.out.push_str(&esc(&dest_url));
            walk.out.push_str("\" alt=\"");
        }
        Tag::HtmlBlock => {}
        Tag::FootnoteDefinition(_) => walk.out.push_str("<div class=\"fn-def\">"),
        Tag::MetadataBlock(_) => {}
        Tag::DefinitionList => walk.out.push_str("<dl>"),
        Tag::DefinitionListTitle => walk.out.push_str("<dt>"),
        Tag::DefinitionListDefinition => walk.out.push_str("<dd>"),
        Tag::Superscript => walk.out.push_str("<sup>"),
        Tag::Subscript => walk.out.push_str("<sub>"),
    }
}

fn end_tag(walk: &mut Walk, tag: TagEnd) {
    match tag {
        TagEnd::Paragraph => walk.out.push_str("</p>"),
        TagEnd::Heading(level) => {
            walk.out.push_str("</h");
            walk.out.push(heading_digit(level));
            walk.out.push('>');
        }
        TagEnd::BlockQuote(_) => walk.out.push_str("</blockquote>"),
        TagEnd::CodeBlock => {}
        TagEnd::HtmlBlock => {}
        TagEnd::List(true) => walk.out.push_str("</ol>"),
        TagEnd::List(false) => walk.out.push_str("</ul>"),
        TagEnd::Item => walk.out.push_str("</li>"),
        TagEnd::FootnoteDefinition => walk.out.push_str("</div>"),
        TagEnd::Table => walk.out.push_str("</table>"),
        TagEnd::TableHead => {
            walk.in_head = false;
            walk.out.push_str("</tr></thead><tbody>");
        }
        TagEnd::TableRow => walk.out.push_str("</tr>"),
        TagEnd::TableCell => walk.out.push_str(if walk.in_head { "</th>" } else { "</td>" }),
        TagEnd::Emphasis => walk.out.push_str("</em>"),
        TagEnd::Strong => walk.out.push_str("</strong>"),
        TagEnd::Strikethrough => walk.out.push_str("</del>"),
        TagEnd::Link => walk.out.push_str("</a>"),
        TagEnd::Image => walk.out.push_str("\"/>"),
        TagEnd::MetadataBlock(_) => {}
        TagEnd::DefinitionList => walk.out.push_str("</dl>"),
        TagEnd::DefinitionListTitle => walk.out.push_str("</dt>"),
        TagEnd::DefinitionListDefinition => walk.out.push_str("</dd>"),
        TagEnd::Superscript => walk.out.push_str("</sup>"),
        TagEnd::Subscript => walk.out.push_str("</sub>"),
    }
}

fn heading_digit(level: HeadingLevel) -> char {
    match level {
        HeadingLevel::H1 => '1',
        HeadingLevel::H2 => '2',
        HeadingLevel::H3 => '3',
        HeadingLevel::H4 => '4',
        HeadingLevel::H5 => '5',
        HeadingLevel::H6 => '6',
    }
}

fn flush_fence(out: &mut String, lang: &str, body: &str) {
    let shown = lang.trim();
    out.push_str("<div class=\"fence\"><header><span>");
    out.push_str(&esc(if shown.is_empty() { "code" } else { shown }));
    out.push_str("</span><button type=\"button\" class=\"copy\">copy</button></header><pre class=\"code\"><code");
    if !shown.is_empty() {
        out.push_str(" class=\"lang-");
        out.push_str(&esc(shown));
        out.push('"');
    }
    out.push('>');
    out.push_str(&highlight(body, shown));
    out.push_str("</code></pre></div>");
}

fn esc(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(ch),
        }
    }
    out
}

fn syntax_set() -> &'static SyntaxSet {
    static SET: OnceLock<SyntaxSet> = OnceLock::new();
    SET.get_or_init(two_face::syntax::extra_newlines)
}

fn tokyo_night() -> &'static Theme {
    static THEME: OnceLock<Theme> = OnceLock::new();
    THEME.get_or_init(|| {
        let bytes = include_bytes!("../../xai-grok-markdown/assets/tokyo-night.tmTheme");
        ThemeSet::load_from_reader(&mut Cursor::new(&bytes[..])).expect("tokyo-night theme")
    })
}

fn highlight(code: &str, lang: &str) -> String {
    let key = lang.trim();
    if key.is_empty() {
        return esc(code);
    }
    let ss = syntax_set();
    let syntax = ss
        .find_syntax_by_token(key)
        .or_else(|| ss.find_syntax_by_extension(key));
    let Some(syntax) = syntax else {
        return esc(code);
    };
    let mut hl = HighlightLines::new(syntax, tokyo_night());
    let mut out = String::with_capacity(code.len() + 64);
    for line in LinesWithEndings::from(code) {
        match hl.highlight_line(line, ss) {
            Ok(ranges) => {
                for (style, text) in ranges {
                    let c = style.foreground;
                    out.push_str("<span style=\"color:#");
                    push_hex(&mut out, c.r);
                    push_hex(&mut out, c.g);
                    push_hex(&mut out, c.b);
                    out.push_str("\">");
                    out.push_str(&esc(text));
                    out.push_str("</span>");
                }
            }
            Err(_) => out.push_str(&esc(line)),
        }
    }
    out
}

fn push_hex(out: &mut String, byte: u8) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    out.push(HEX[(byte >> 4) as usize] as char);
    out.push(HEX[(byte & 0x0f) as usize] as char);
}

#[cfg(test)]
mod tests {
    use super::html;

    #[test]
    fn python_fence_uses_syntect_colors() {
        let got = html("```python\ndef hi():\n  return 1\n```");
        assert!(got.contains("style=\"color:#"), "{got}");
        assert!(got.contains("fence"), "{got}");
        assert!(got.contains("def"), "{got}");
        assert!(got.contains("return"), "{got}");
    }

    #[test]
    fn indented_fence_still_parses() {
        let got = html("   ```js\nconst x = 1;\n```");
        assert!(got.contains("const"), "{got}");
        assert!(got.contains("fence"), "{got}");
    }

    #[test]
    fn linked_url() {
        let got = html("see [x](https://example.com/x)");
        assert!(got.contains("href=\"https://example.com/x\""), "{got}");
        assert!(got.contains(">x</a>"), "{got}");
    }

    #[test]
    fn angle_autolink() {
        let got = html("see <https://example.com/x>");
        assert!(got.contains("href=\"https://example.com/x\""), "{got}");
    }

    #[test]
    fn double_tilde_strikes_single_does_not() {
        let got = html("keep ~~this~~ but not ~that~");
        assert!(got.contains("<del>this</del>"), "{got}");
        assert!(!got.contains("<del>that"), "{got}");
        assert!(got.contains("~that"), "{got}");
    }

    #[test]
    fn percent_tilde_is_not_strike() {
        let got = html("only: ~**10%** (~**300**)");
        assert!(!got.contains("<del>"), "{got}");
        assert!(got.contains("10%"), "{got}");
    }

    #[test]
    fn heading_and_list() {
        let got = html("# Title\n\n- one\n- two\n");
        assert!(got.contains("<h1>Title</h1>"), "{got}");
        assert!(got.contains("<ul>"), "{got}");
        assert!(got.contains("<li>one</li>"), "{got}");
    }

    #[test]
    fn table_head_is_th() {
        let got = html("| a | b |\n| --- | --- |\n| 1 | 2 |\n");
        assert!(got.contains("<th>a</th>") || got.contains("<th>a"), "{got}");
        assert!(got.contains("<td>1</td>") || got.contains("<td>1"), "{got}");
        assert!(!got.contains("<thead><tr><td>"), "{got}");
    }
}
