/**
 * Containment for a defect in the generated transport.
 *
 * `APIPromise` (src/generated/types/async.ts) eagerly derives a second promise
 * in its constructor:
 *
 *     this.#unwrapped = this.#promise.then(([value]) => value);
 *
 * `.then()` / `await` read `#promise`, so `#unwrapped` never gets a rejection
 * handler on the path every generated method actually uses. When the request
 * pipeline *throws* rather than returning an error `Result` - a response that
 * declares `content-type: application/json` and carries a truncated or empty
 * body is enough, and so is a request payload JSON cannot encode - `#unwrapped`
 * rejects with nobody listening. Node's default policy for an unhandled
 * rejection is to terminate the process.
 *
 * Verified against a real server on a real socket: the SDK refuses the call
 * correctly (fail-closed holds), and then the host process exits 1 a tick
 * later. A guardrail that kills the application it protects when the control
 * plane sends a short read is not acceptable, and this SDK only started making
 * these calls when `control()` became real.
 *
 * The fix belongs upstream in the generator, so nothing here edits generated
 * code. Holding the `APIPromise` and attaching a no-op handler to `#unwrapped`
 * before awaiting it is enough, and it changes nothing about the value or the
 * error the caller sees. Delete this module the day `APIPromise` derives
 * `#unwrapped` lazily.
 */
import type { APIPromise } from "./generated/types/async";
import type { Result } from "./generated/types/fp";
import { unwrapAsync } from "./generated/types/fp";

export async function settle<T>(pending: APIPromise<Result<T, unknown>>): Promise<T> {
  // Reaches #unwrapped, which is the promise nobody else claims.
  void pending.catch(() => undefined);
  return unwrapAsync(pending);
}
