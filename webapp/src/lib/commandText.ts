/**
 * Command text that is really a table.
 *
 * Several commands return `kind: "text"` with columnar, newline-separated output
 * - `/help` is the obvious one, listing every command and then every keybinding.
 * Markdown collapses those single newlines into one paragraph, so the whole help
 * text arrives as a single unreadable run-on sentence.
 *
 * The cleanest fix is `/help` returning `kind: "list"` from the core command
 * registry, which is a core change and out of scope for the web client. Until
 * then, this module recognises the columnar shape on the client and renders it
 * as a structured list instead of handing it to markdown. Anything that is not
 * columnar passes straight through to markdown untouched.
 */

export interface CommandTextRow {
  /** Left column, e.g. "/compact" or "Ctrl+O". */
  label: string;
  /** Right column, e.g. "Compact the conversation now". */
  detail: string;
}

export interface CommandTextSection {
  /** A "Heading:" line that preceded these rows, if there was one. */
  heading: string | null;
  rows: CommandTextRow[];
}

export interface CommandTextDocument {
  /** Lines before the first columnar row, kept as markdown. */
  intro: string | null;
  sections: CommandTextSection[];
}

/** Two or more spaces separate the columns; a single space is prose. */
const COLUMN_GAP = /\s{2,}/;

/** A line that is a label followed by a description, e.g. "/compact  Compact now". */
function parseRow(line: string): CommandTextRow | null {
  const trimmed = line.trim();
  if (trimmed === "") return null;

  const gap = COLUMN_GAP.exec(trimmed);
  if (gap) {
    const label = trimmed.slice(0, gap.index).trim();
    const detail = trimmed.slice(gap.index).trim();
    if (label !== "" && detail !== "") return { label, detail };
  }

  // The server pads its columns to a fixed width, so a value longer than the
  // column swallows the gap entirely: "/settings [key val]Show or change …" and
  // "Ctrl+J / \+Enter Insert newline" both arrive with one space, or none.
  if (!trimmed.startsWith("/") && !trimmed.includes("+")) return null;

  const bracket = /^(.*?\])(\S.*)$/.exec(trimmed);
  if (bracket?.[1] !== undefined && bracket[2] !== undefined) {
    return { label: bracket[1], detail: bracket[2].trim() };
  }

  // Last boundary that is followed by a capitalised description.
  const capitalised = /^(.*\S)\s+([A-Z].*)$/.exec(trimmed);
  if (capitalised?.[1] !== undefined && capitalised[2] !== undefined) {
    return { label: capitalised[1], detail: capitalised[2] };
  }

  return null;
}

function isHeading(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.length > 1 && trimmed.endsWith(":") && !COLUMN_GAP.test(trimmed);
}

/**
 * Parse columnar command output.
 *
 * Returns `null` when the text is ordinary prose, which is the signal to render
 * it as markdown exactly as before.
 */
export function parseCommandText(body: string): CommandTextDocument | null {
  const lines = body.split("\n");
  const parsed = lines.map((line) => (isHeading(line) || line.trim() === "" ? null : parseRow(line)));

  // Prose stays prose: a table needs a run of columnar lines, not one accident.
  const columnarLines = parsed.filter((row) => row !== null).length;
  if (columnarLines < 3) return null;

  const intro: string[] = [];
  const sections: CommandTextSection[] = [];
  let current: CommandTextSection | null = null;
  let sawFirstRow = false;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const row = parsed[index] ?? null;

    if (!sawFirstRow && row === null) {
      if (line.trim() !== "") intro.push(line);
      continue;
    }

    if (row !== null) {
      sawFirstRow = true;
      if (current === null) {
        current = { heading: null, rows: [] };
        sections.push(current);
      }
      current.rows.push(row);
      continue;
    }

    if (isHeading(line)) {
      current = { heading: line.trim().replace(/:$/, ""), rows: [] };
      sections.push(current);
      continue;
    }

    // A blank line ends the current block; the next row starts a fresh one.
    if (line.trim() === "") {
      current = null;
      continue;
    }

    // Kept rather than dropped: a line that is neither a heading nor two columns
    // is still content the user asked to see.
    if (current === null) {
      current = { heading: null, rows: [] };
      sections.push(current);
    }
    current.rows.push({ label: line.trim(), detail: "" });
  }

  const populated = sections.filter((section) => section.rows.length > 0);
  if (populated.length === 0) return null;

  return { intro: intro.length > 0 ? intro.join("\n") : null, sections: populated };
}
