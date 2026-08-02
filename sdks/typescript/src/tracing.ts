/** OpenTelemetry-compatible trace/span id generation for control events. */

/** All-zero ids are invalid per OTEL, so they read as "no trace context". */
export const FALLBACK_TRACE_ID = "0".repeat(32);
export const FALLBACK_SPAN_ID = "0".repeat(16);

function randomHex(byteLength: number): string {
  const bytes = new Uint8Array(byteLength);
  globalThis.crypto.getRandomValues(bytes);
  let out = "";
  for (const byte of bytes) {
    out += byte.toString(16).padStart(2, "0");
  }
  return out;
}

/** 128-bit trace id as 32 lowercase hex chars. */
export function generateTraceId(): string {
  return randomHex(16);
}

/** 64-bit span id as 16 lowercase hex chars. */
export function generateSpanId(): string {
  return randomHex(8);
}
