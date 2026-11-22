/**
 * Command text: the columnar output some commands return, and the rules that
 * decide when a `/` draft has stopped being a command and gone back to being a
 * prompt.
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

/* ------------------------------------------------------------ slash drafts */

/**
 * Telling a command from a prompt.
 *
 * `/` on an empty composer opens the command palette, which is the documented
 * affordance -- but from then on the palette's filter is what receives the
 * keystrokes, so the composer no longer sees the draft. People ask about paths,
 * regexes and ratios ("/etc/hosts says what?", "/^a+b$/ is greedy?"), and every
 * one of those opens a palette that used to swallow the question and run
 * `/etc/hosts` as a command. So the palette watches what it is being typed and
 * decides, as early as it honestly can, that this is prose.
 */

/** What a command name can be spelled with. Nothing else can continue one. */
const NAME_CHARACTER = /[a-z0-9_-]/i;

/**
 * True once the query cannot be a command any more.
 *
 * The rule is deliberately early and local: a query whose first word is still a
 * prefix of a real command name stays open (that is a command being typed),
 * and anything else needs a reason to keep going. A space after a word that is
 * not a command, or a character a name cannot contain -- `/` in `/etc/hosts`
 * cannot be beaten -- means the person is writing a sentence.
 *
 * Names, not the fuzzy ranking, on purpose: ranking also matches descriptions, so
 * "background bash jobs" would keep the palette open and swallow the sentence.
 * The cost of that choice is that searching by description with a space in it now
 * hands the text back to the composer; the cost of the other rule was running a
 * command nobody asked for.
 */
export function isProseQuery(query: string, commandNames: readonly string[]): boolean {
  if (query === "") return false;
  const token = query.split(/\s/)[0] ?? "";
  // A space before any name at all: nothing to complete, and nothing to run.
  if (token === "") return true;
  const lower = token.toLowerCase();
  if (commandNames.some((name) => name.toLowerCase().startsWith(lower))) return false;
  const hasSpace = /\s/.test(query);
  const nameLike = [...token].every((character) => NAME_CHARACTER.test(character));
  return hasSpace || !nameLike;
}

/**
 * The composer's draft is the composer's own state, and the palette cannot write
 * to it: while the palette is open, *it* is what is being typed into. When the
 * text turns out not to be a command, the palette closes and hands it back
 * through this event, which is the one thing two sibling components share.
 *
 * A DOM event rather than a module-level mailbox: nothing to reset between
 * tests or sessions, and no singleton left holding somebody's draft.
 */
export const PROMPT_HANDOFF_EVENT = "quartz:prompt-handoff";

/** Give the palette's filter text back to the composer, leading slash and all. */
export function handOffPrompt(query: string): void {
  window.dispatchEvent(new CustomEvent<string>(PROMPT_HANDOFF_EVENT, { detail: `/${query}` }));
}

/** Read a handoff event, or null for anything else that reaches the listener. */
export function readPromptHandoff(event: Event): string | null {
  const detail = (event as CustomEvent<unknown>).detail;
  return typeof detail === "string" ? detail : null;
}
