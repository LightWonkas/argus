// Argus dashboard — ESLint flat config (ESLint 9 + typescript-eslint).
// Frontend quality gate: TS + React Hooks rules. Run with `npm run lint`.

import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  // Generated/external files are not linted.
  { ignores: ["dist", "node_modules", "coverage"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser, ...globals.es2022 },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Vite HMR: component files should only export components (constant exports allowed).
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Unused variable = error; "_"-prefixed arguments are intentional (DI signatures) → exempt.
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
  // Test files: Node + test globals.
  {
    files: ["**/*.test.{ts,tsx}", "**/test/**"],
    languageOptions: { globals: { ...globals.node } },
  },
);
