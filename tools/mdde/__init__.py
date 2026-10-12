"""mdde - translation pipeline for the German Millennium Dawn localisation submod.

Modules:
    config      runtime configuration from environment variables
    locfile     read/write HOI4 localisation files (BOM, ``key:0 "value"``)
    scope       find "open" (untranslated) texts per scope
    cache       append-only translation cache + reject log
    validate    token/format checks for model output
    openrouter  OpenRouter chat-completions client with model chain
    translate   batch runner (parallel, budget, second round)
    build       generate the submod's localisation files + check them
    install     register the submod with the native Paradox launcher
"""

__version__ = "1.0.0"
