use std::io::{self, Write as _};

use crossterm::{cursor::MoveTo, queue, style::Print};
use ratatui::layout::Rect;

use crate::{common::TerminalLike, segment::split_into_line_segments};

/// Handles terminal resize by completely re-rendering the scrollback history.
///
/// This function uses a "nuclear option" approach: it sends RIS (Reset to Initial State)
/// to clear the entire terminal, then re-outputs all scrollback history and positions
/// the viewport appropriately.
///
/// # Why this approach?
///
/// When the terminal is resized, text reflow happens automatically *before* our application
/// receives the resize signal (SIGWINCH). This creates several problems:
///
/// 1. **Scrollback corruption**: The built-in `terminal.autoresize()` doesn't handle reflowed
///    content properly, often damaging scrollback history or leaving visual artifacts.
///
/// 2. **Viewport artifacts**: The old viewport borders get reflowed along with regular text,
///    appearing as garbage above the new viewport position. While we could try to move the
///    viewport up to avoid this, it becomes impossible when the viewport is already near the top.
///
/// 3. **Unpredictable reflow**: Different terminals handle text reflow differently, making it
///    nearly impossible to predict exactly where content will end up after resize. We tried
///    calculating reflow based on character counts, but edge cases and terminal-specific
///    behaviors made this unreliable.
///
/// The RIS + re-render approach is more drastic but provides consistency across all terminals
/// and resize scenarios. It's especially important for horizontal resizing where text reflow
/// is most problematic.
///
/// # Arguments
///
/// * `terminal` - The terminal instance to resize
/// * `history` - The complete scrollback history (with CRLF line endings)
///
/// # Returns
///
/// Returns `Ok(())` on success, or an I/O error if terminal operations fail.
pub fn resize_purge_rerender<T: TerminalLike>(terminal: &mut T, history: &str) -> io::Result<()> {
    let viewport = terminal.viewport_area();
    let size = terminal.size()?;

    // Clear current screen, clear scrollbackhistory and move the cursor to the top left corner
    // note: we could've also used RIS (\x1bc) hard reset, but it doesn't clear scrollback in iterm/terminal.app
    terminal.writer_mut().write_all(b"\x1b[2J\x1b[3J\x1b[H")?;
    terminal.writer_mut().flush()?;

    // Count newlines in history as a quick check for whether we have enough content
    // The +1 accounts for content on the first line (before any newlines)
    let num_newlines = 1 + history
        .as_bytes()
        .iter()
        .filter(|&&c| c == b'\n')
        .take(size.height.into()) // Only count up to screen height for efficiency
        .count() as u16;

    // Re-output the entire scrollback history
    queue!(terminal.writer_mut(), Print(history))?;

    // Add blank lines to reserve space for the viewport
    for _ in 0..viewport.height {
        queue!(terminal.writer_mut(), Print("\r\n"))?;
    }

    // Calculate where to position the viewport
    let viewport_y = if num_newlines + viewport.height >= size.height {
        // We have enough content to fill the screen, viewport goes at the bottom
        size.height.saturating_sub(viewport.height)
    } else {
        // Not enough content to fill the screen, need to calculate exact position
        // Use split_into_line_segments to account for line wrapping
        let segments = split_into_line_segments(history, size.width.into());
        let num_visible_lines = segments.len().min(u16::MAX as _) as u16;

        // Position viewport right after the content, but not beyond screen bottom
        num_visible_lines.min(size.height.saturating_sub(viewport.height))
    };

    // Flush all queued commands
    terminal.writer_mut().flush()?;

    // Resize and clear the viewport
    terminal.set_viewport_area(ratatui::layout::Rect {
        x: 0,
        y: viewport_y,
        width: size.width,
        height: viewport.height,
    });
    terminal.clear()?;

    Ok(())
}

/// Resize the viewport to a new height with terminal dimensions being the same.
///
/// When shrinking: Always anchors to top (gap appears at bottom)
/// When growing: Tries to expand down first, then pushes content up if needed
pub fn resize_viewport_height<T: TerminalLike>(
    terminal: &mut T,
    new_height: u16,
) -> io::Result<()> {
    macro_rules! queue {
        ($($command:expr),* $(,)?) => {{
            $(crossterm::queue!(terminal.writer_mut(), $command)?;)*
            Ok::<(), io::Error>(())
        }};
    }

    let size = terminal.size()?;
    let current_viewport = terminal.viewport_area();
    let old_height = current_viewport.height;

    if new_height == old_height {
        return Ok(());
    }

    // Ensure new height is valid
    if new_height == 0 || new_height >= size.height {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!(
                "Invalid viewport height: {} (terminal height: {})",
                new_height, size.height
            ),
        ));
    }

    if new_height > old_height {
        // Growing: Smart expansion - try to expand down first, then push content up if needed
        let growth = new_height - old_height;
        let bottom_edge = current_viewport.y + current_viewport.height;
        let space_below = size.height.saturating_sub(bottom_edge);

        // Calculate the new y position
        let new_y = if space_below >= growth {
            // We have enough space below - expand down, keep same y
            current_viewport.y
        } else {
            // Need to push content up
            // Either use all space below and push up the rest, or anchor to bottom
            if space_below > 0 {
                // Use available space below and push up for the remainder
                current_viewport.y.saturating_sub(growth - space_below)
            } else {
                // Already at bottom, push everything up
                size.height.saturating_sub(new_height)
            }
        };

        // If we need to scroll content up
        if new_y < current_viewport.y {
            let scroll_amount = current_viewport.y - new_y;

            // Move to bottom and emit newlines to push content into scrollback
            queue!(MoveTo(0, size.height - 1))?;
            for _ in 0..scroll_amount {
                queue!(Print("\r\n"))?;
            }
            terminal.writer_mut().flush()?;
        }

        // Clear the old viewport
        terminal.clear()?;

        // Set the new viewport area
        terminal.set_viewport_area(Rect::new(0, new_y, current_viewport.width, new_height));
    } else {
        // Shrinking: Always anchor to top (gap appears at bottom)
        terminal.clear()?;
        terminal.set_viewport_area(Rect::new(
            0,
            current_viewport.y,
            current_viewport.width,
            new_height,
        ));
    }

    Ok(())
}

