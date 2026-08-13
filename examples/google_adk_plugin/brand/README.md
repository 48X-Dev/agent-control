# Brand templates

Drop a `.pptx` here and point `AGENT_CONTROL_AGENT_FILE_TEMPLATE_PPTX` at its
path inside the image (`/app/examples/google_adk_plugin/brand/<name>.pptx`).
Decks the agent writes then carry this deployment's theme, fonts and slide size
instead of python-pptx's 4:3 Calibri default.

Build it by opening a real deck and deleting every slide: masters, layouts,
theme and slide size survive, content does not. A template that keeps its slides
prepends all of them to every deck the agent writes.

The `.pptx` files here are gitignored. A brand deck is the deployment's, not
this repository's.
