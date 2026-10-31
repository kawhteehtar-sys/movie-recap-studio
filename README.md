# Movie Recap Studio (Personal API Key)

## Run
Open `index.html` in a modern browser, enter your Gemini API key, and choose a public YouTube URL or upload a video.

## Deploy free
1. Create a GitHub repository and upload `index.html`.
2. In Cloudflare Dashboard, open Workers & Pages -> Create -> Pages -> Connect to Git.
3. Select the repository and deploy. No build command; output directory is `/` (or the project root, depending on the setup UI).
4. Open the `*.pages.dev` URL.

## Notes
- The app calls the Gemini API directly from the browser. Your API key is stored in this browser's localStorage if you press Save. Do not use this on a shared device or publish a key in the source code.
- This is a personal-use starter. Anyone who opens the deployed page can enter their own key; keys are not shared with the site owner.
- Gemini free-tier usage has model-specific rate limits and quotas. "Unlimited" here means the app does not impose a daily count cap; Google may still throttle or limit requests.
- YouTube URL support is currently documented as preview and only works for public videos. Limits and availability can change.
- Video file upload uses Gemini Files API. Large video uploads can fail if browser/network/API constraints intervene.
- Text-to-speech uses the browser/device voice and does not export an MP3.
- This version creates recap scripts; it does not yet render a finished video or generate a downloadable voice track.
