/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          base: '#0c0c0f',
          surface: '#141418',
          card: '#1a1a22',
          elevated: '#222230',
        },
        amber: {
          DEFAULT: '#F5A623',
          dark: '#E8912D',
          glow: 'rgba(245, 166, 35, 0.4)',
          subtle: 'rgba(245, 166, 35, 0.1)',
        },
        cream: {
          DEFAULT: '#F5E6D0',
          muted: 'rgba(245, 230, 208, 0.6)',
        },
        status: {
          pending: '#6B7280',
          generating: '#F5A623',
          review: '#3B82F6',
          approved: '#10B981',
          rejected: '#EF4444',
          unsatisfactory: '#991B1B',
        },
      },
      fontFamily: {
        serif: ['"Noto Serif SC"', 'serif'],
        sans: ['"DM Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      animation: {
        'pulse-amber': 'pulseAmber 2s ease-in-out infinite',
        'scan-line': 'scanLine 1.5s ease-in-out infinite',
        'film-reveal': 'filmReveal 0.6s ease-out forwards',
        'glow-pulse': 'glowPulse 2s ease-in-out infinite',
        'slide-up': 'slideUp 0.5s ease-out forwards',
        'fade-in': 'fadeIn 0.4s ease-out forwards',
      },
      keyframes: {
        pulseAmber: {
          '0%, 100%': { boxShadow: '0 0 8px 2px rgba(245, 166, 35, 0.3)' },
          '50%': { boxShadow: '0 0 20px 6px rgba(245, 166, 35, 0.6)' },
        },
        scanLine: {
          '0%': { transform: 'translateY(-100%)', opacity: '0' },
          '50%': { opacity: '0.6' },
          '100%': { transform: 'translateY(100%)', opacity: '0' },
        },
        filmReveal: {
          '0%': { opacity: '0', transform: 'translateY(30px) scale(0.95)' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        glowPulse: {
          '0%, 100%': { borderColor: 'rgba(245, 166, 35, 0.3)' },
          '50%': { borderColor: 'rgba(245, 166, 35, 0.8)' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(20px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
      },
      boxShadow: {
        'amber-glow': '0 0 20px rgba(245, 166, 35, 0.3)',
        'amber-glow-lg': '0 0 40px rgba(245, 166, 35, 0.4)',
        'card': '0 4px 24px rgba(0, 0, 0, 0.4)',
        'card-hover': '0 8px 40px rgba(0, 0, 0, 0.6)',
      },
    },
  },
  plugins: [],
}