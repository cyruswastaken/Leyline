import js from "@eslint/js";
import tseslint from "typescript-eslint";

// Text from users, documents and the model must only ever reach the page as
// text. These rules make the unsafe ways of doing otherwise a lint error, and
// tests/test_regressions.py scans the sources for the same sinks.
const unsafeSinks = [
  ["innerHTML", "Use textContent or createElement; never build markup from data."],
  ["outerHTML", "Use textContent or createElement; never build markup from data."],
  ["insertAdjacentHTML", "Use textContent or createElement; never build markup from data."],
  ["insertAdjacentText", "Use el() from lib/dom.ts."],
  ["srcdoc", "Never load markup into a frame."],
];

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    files: ["src/**/*.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        ...unsafeSinks.map(([name, message]) => ({
          selector: `MemberExpression[property.name='${name}']`,
          message,
        })),
        {
          selector: "CallExpression[callee.object.name='document'][callee.property.name=/^(write|writeln)$/]",
          message: "document.write is forbidden.",
        },
        {
          selector: "CallExpression[callee.name='eval']",
          message: "eval is forbidden (the CSP forbids it too).",
        },
        {
          selector: "NewExpression[callee.name='Function']",
          message: "new Function is forbidden (the CSP forbids it too).",
        },
        {
          selector:
            "CallExpression[callee.name='el'][arguments.0.value=/^(a|img|iframe|script|object|embed|link)$/]",
          message: "Links, images, frames and scripts are never created from the client; use the server-rendered template.",
        },
        {
          selector:
            "CallExpression[callee.property.name='createElement'][arguments.0.value=/^(a|img|iframe|script|object|embed|link)$/]",
          message: "Links, images and frames are never created from the client; model text must not become them.",
        },
      ],
    },
  },
);
