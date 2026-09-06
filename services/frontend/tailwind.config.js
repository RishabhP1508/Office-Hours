/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        espresso: "#241B12",
        "espresso-2": "#30251A",
        ink: "#241C13",
        body: "#4A3E33",
        paper: "#FFFFFF",
        page: "#EFECE6",
        rule: "#E2DDD5",
        saffron: "#D98324",
        "saffron-lit": "#E8A33D",
        link: "#9A5312",
        sand: "#F8F2E8",
        muted: "#7A6E62",
      },
      fontFamily: {
        serif: ["var(--font-plex-serif)", "Georgia", "serif"],
        sans: ["var(--font-plex-sans)", "system-ui", "-apple-system", "sans-serif"],
      },
    },
  },
  plugins: [],
};
