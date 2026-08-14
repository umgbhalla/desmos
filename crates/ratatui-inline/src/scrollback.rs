use std::io::{self, Write};

use crossterm::{cursor::MoveTo, style::Print};
use ratatui::layout::Rect;

use crate::{common::TerminalLike, segment::split_into_line_segments};

// ANSI escape sequence constants.
// CSI J with the default parameter (0): erase from cursor to end of display.
// Byte-identical to what the previous termwiz constant
// (`CSI::Edit(Edit::EraseInDisplay(EraseInDisplay::EraseToEndOfDisplay))`)
// rendered, and to crossterm's `Clear(ClearType::FromCursorDown)`.
const ANSI_CLEAR_FROM_CURSOR_DOWN: &str = "\x1b[J";

pub fn emit_to_scrollback<T: TerminalLike>(terminal: &mut T, content: &str) -> io::Result<()> {
    macro_rules! queue {
        ($($command:expr),* $(,)?) => {{
            $(crossterm::queue!(terminal.writer_mut(), $command)?;)*
            Ok::<(), io::Error>(())
        }};
    }

    let size = terminal.size()?;
    let viewport_area = terminal.viewport_area();
    let terminal_width = size.width as usize;
    debug_assert!(viewport_area.bottom() <= size.height);

    // Use zero-copy line segmentation
    let segments = split_into_line_segments(content, terminal_width);

    // Calculate where viewport will end up after content
    let new_viewport_y =
        (viewport_area.y + segments.len() as u16).min(size.height - viewport_area.height);

    // Position from viewport top and clear from this position down
    queue!(
        MoveTo(0, viewport_area.y),
        Print(ANSI_CLEAR_FROM_CURSOR_DOWN),
    )?;

    // Now print the content
    queue!(MoveTo(0, viewport_area.y))?;
    for segment in &segments {
        queue!(Print(segment))?; // this already includes crlfs if there's any
    }

    // Create exact viewport space
    for _ in 0..viewport_area.height {
        queue!(Print("\r\n"))?;
    }

    // Clear the new viewport area for rendering
    queue!(
        MoveTo(0, new_viewport_y),
        Print(ANSI_CLEAR_FROM_CURSOR_DOWN),
    )?;

    // We'll flush by default; the caller is expected to have this in sync block anyway
    terminal.writer_mut().flush()?;

    // Reset the back buffer so next render knows viewport is empty
    terminal.reset_back_buffer();

    // Reposition viewport if needed
    if new_viewport_y != viewport_area.y {
        terminal.set_viewport_area(Rect {
            y: new_viewport_y,
            ..viewport_area
        });
    }

    Ok(())
}

