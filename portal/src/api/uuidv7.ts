/**
 * UUIDv7 (RFC 9562 section 5.7): 48-bit Unix milliseconds, version 7,
 * 74 random bits. Used as the Idempotency-Key on every POST (portal-api.md 4.2).
 * Keys made in the same millisecond stay ordered by a 12-bit counter.
 */
let lastMs = -1;
let counter = 0;

export function uuidv7(now: number = Date.now()): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);

  if (now <= lastMs) {
    counter = (counter + 1) & 0xfff;
    if (counter === 0) lastMs += 1; // counter wrapped: borrow the next millisecond
    now = lastMs;
  } else {
    lastMs = now;
    counter = (bytes[6] ?? 0) & 0x07; // random start, leaves room to count up
  }

  // 48-bit big-endian timestamp. Bit operators are 32-bit, so split it.
  const high = Math.floor(now / 2 ** 16);
  const low = now % 2 ** 16;
  bytes[0] = (high >>> 24) & 0xff;
  bytes[1] = (high >>> 16) & 0xff;
  bytes[2] = (high >>> 8) & 0xff;
  bytes[3] = high & 0xff;
  bytes[4] = (low >>> 8) & 0xff;
  bytes[5] = low & 0xff;
  bytes[6] = 0x70 | ((counter >>> 8) & 0x0f); // version 7 + counter high nibble
  bytes[7] = counter & 0xff;
  bytes[8] = 0x80 | ((bytes[8] ?? 0) & 0x3f); // variant 10

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export const UUIDV7_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
