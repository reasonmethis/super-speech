/// <reference types="vite/client" />

import type { DesktopApi } from "./runtime";

declare global {
  interface Window {
    superSpeech?: DesktopApi;
  }
}
