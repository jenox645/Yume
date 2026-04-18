import js from "@eslint/js";

export default [
    js.configs.recommended,
    {
        files: ["extension/**/*.js"],
        ignores: ["extension/js/wanakana_min.js"],
        languageOptions: {
            ecmaVersion: 2022,
            sourceType: "script",
            globals: {
                // Browser
                window: "readonly",
                document: "readonly",
                navigator: "readonly",
                console: "readonly",
                setTimeout: "readonly",
                setInterval: "readonly",
                clearTimeout: "readonly",
                clearInterval: "readonly",
                fetch: "readonly",
                URL: "readonly",
                Blob: "readonly",
                FormData: "readonly",
                Headers: "readonly",
                Request: "readonly",
                Response: "readonly",
                AbortController: "readonly",
                AbortSignal: "readonly",
                CustomEvent: "readonly",
                MediaRecorder: "readonly",
                AudioContext: "readonly",
                MediaStreamAudioSourceNode: "readonly",
                HTMLVideoElement: "readonly",
                HTMLAudioElement: "readonly",
                MutationObserver: "readonly",
                ResizeObserver: "readonly",
                IntersectionObserver: "readonly",
                location: "readonly",
                performance: "readonly",
                crypto: "readonly",
                structuredClone: "readonly",
                requestAnimationFrame: "readonly",
                cancelAnimationFrame: "readonly",
                // Chrome extension APIs
                chrome: "readonly",
                URLSearchParams: "readonly",
                history: "readonly",
                CSS: "readonly",
                // Extension script globals (injected via manifest)
                DEBUG: "readonly",
                AudioCapture: "readonly",
                SubtitleWindow: "readonly",
                // Vendored libraries
                wanakana: "readonly",
            },
        },
        rules: {
            "no-undef": "error",
            "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" }],
            "eqeqeq": ["warn", "smart"],
            "no-debugger": "warn",
            "no-empty": ["warn", { allowEmptyCatch: true }],
        },
    },
    {
        ignores: ["node_modules/", "extension/js/wanakana_min.js"],
    },
];
