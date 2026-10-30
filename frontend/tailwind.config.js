/** Design tokens from the brief. Use these names in components; no ad-hoc colours. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        page: "#F7F8FA",
        line: "#E4E7EC",
        divider: "#F0F1F4",
        ink: "#101828",
        body: "#344054",
        muted: "#475467",
        accent: "#4338CA",
        "accent-bg": "#EEF0FF",
        "accent-text": "#3730A3",
        ok: "#067647", "ok-bg": "#ECFDF3",
        warn: "#B54708", "warn-bg": "#FFFAEB",
        bad: "#B42318", "bad-bg": "#FEF3F2", "bad-line": "#FDA29B",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      maxWidth: { content: "1040px" },
    },
  },
  plugins: [],
};
