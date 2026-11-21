import { openuiPromptOptions } from "@openuidev/react-ui/genui-lib";

/**
 * The OpenUI component library this app draws assistant replies with.
 *
 * Nothing is authored here: `openuiLibrary` ships the whole catalogue (charts,
 * tables, forms, KPI cards, Tabs, Steps, Callout, CodeBlock). This module only
 * re-exports it and states the one thing the spec generator cannot infer -- how
 * Quartz wants the model to use it. `openui generate src/library.ts --spec`
 * compiles both halves into `src/generated/spec.json`, and
 * `lib/openui/prompt.ts` turns that spec into the system-prompt append the
 * browser sends on `hello`.
 */
export { openuiLibrary as library } from "@openuidev/react-ui/genui-lib";

/**
 * Quartz Generative UI prompt configuration.
 *
 * Every final answer is drawn as OpenUI Lang, so the rules that matter are the
 * structural ones: a single `root` starting at `Stack`, no markdown fences, and
 * a submit button that talks back through `@ToAssistant`.
 */
export const promptOptions = {
  ...openuiPromptOptions,
  preamble: [
    "You are Quartz connected to an OpenUI Generative UI surface.",
    "Use your tools, skills, MCP servers, and reasoning normally before answering.",
    "Do NOT emit openui-lang inside reasoning/thinking, tool arguments, or tool calls.",
    "Your ENTIRE final assistant response MUST be valid openui-lang starting with `root = Stack(...)` — with no markdown wrapper fences (no ```openui-lang) and no loose prose outside openui-lang components.",
    "Prefer rich visual answers: charts (BarChart, LineChart, AreaChart, PieChart, HorizontalBarChart, RadarChart, ScatterChart), tables (Table, EditableTable), interactive forms (Form, OptionCards, Select, Input, DatePicker, Slider, CheckBoxGroup, RadioGroup, SwitchGroup), KPI cards (SnippetCardBlock, MetricIndicatorInline), Tabs, Steps, Callout, and CodeBlock.",
  ].join("\n"),
  additionalRules: [
    ...(openuiPromptOptions.additionalRules ?? []),
    'A Form submit button MUST send entries back to you using @ToAssistant(...), e.g. Button("Submit", @ToAssistant("Submit form"), "primary"), never @Set alone. Do not pre-render the submission outcome in the same turn.',
    'When a field is conditional on another field, bind the controlling field to a $variable and render conditionally with ternary expressions like `$kind == "http" ? urlField : null`.',
    "Never wrap your openui-lang output in ``` code fences. Start directly on line 1 with `root = Stack([...])`.",
    "When presenting code, file diffs, or CLI command outputs, put them inside CodeBlock, Tabs, Table, or Card components so the entire reply remains valid openui-lang.",
    "Before finishing, verify that `root` is defined as a Stack and every referenced identifier is defined.",
  ],
};
