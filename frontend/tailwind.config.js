/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "media",
  theme: {
    extend: {
      colors: {
        ink: { 50: "#f6f7f9", 100: "#eceef2", 200: "#d5d9e1", 400: "#8b93a5", 600: "#4a5163", 800: "#222633", 900: "#14161f" },
        brand: { 500: "#e8590c", 600: "#c94a08" },
      },
      fontFamily: { sans: ["Inter", "Noto Sans Devanagari", "system-ui", "sans-serif"] },
    },
  },
  plugins: [],
};
