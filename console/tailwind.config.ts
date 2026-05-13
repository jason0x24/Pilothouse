import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          900: "#0a0e14",
          800: "#11161f",
          700: "#1a2030",
          600: "#252d40",
          500: "#3a4257",
          400: "#5c6680",
          300: "#8590a8",
          200: "#b8bfd1",
          100: "#e6eaf2",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
