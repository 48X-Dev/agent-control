import type { Halt, Nudge, SessionMessage } from '@/core/api/types';

/**
 * Things that happened to a turn but are not in the executor's transcript.
 *
 * Neither a nudge nor a stop leaves a record the executor keeps. A nudge is
 * appended to a model *request*, which is not an event; a stop replaces a
 * response, and the executor cannot tell a blocked response from ordinary
 * model output. So the panel renders both from Agent Control's own rows and
 * never by pattern-matching transcript text - which would also mean an agent
 * could forge either one by saying the right sentence.
 */
export type TranscriptAnnotation =
  | {
      kind: 'nudge';
      id: number;
      /** When it landed, used only to place it among the messages. */
      at: number | null;
      /** The exact text the model was shown. */
      body: string;
      status: Nudge['status'];
      rejectedByControl: string | null;
    }
  | {
      kind: 'halt';
      id: number;
      at: number | null;
      mode: Halt['mode'];
      boundary: Halt['applied_at_boundary'];
      toolName: string | null;
      /** Whether the turn this stop was bound to has actually ended. */
      ended: boolean;
    };

export type TranscriptItem =
  | { kind: 'message'; key: string; message: SessionMessage }
  | { kind: 'annotation'; key: string; annotation: TranscriptAnnotation };

function toTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const at = new Date(value).getTime();
  return Number.isNaN(at) ? null : at;
}

/**
 * Annotations for the nudges that actually reached a model, plus the ones a
 * control refused.
 *
 * Queued and cancelled nudges are deliberately absent: nothing happened in the
 * conversation, and putting them in the transcript would show the agent being
 * told something it was never told. They live in the queue list instead.
 */
export function nudgeAnnotations(nudges: Nudge[]): TranscriptAnnotation[] {
  return nudges
    .filter(
      (nudge) => nudge.status === 'applied' || nudge.status === 'rejected'
    )
    .map((nudge) => ({
      kind: 'nudge' as const,
      id: nudge.id,
      at: toTime(nudge.applied_at ?? nudge.created_at),
      body: nudge.body,
      status: nudge.status,
      rejectedByControl: nudge.rejected_by_control ?? null,
    }));
}

/**
 * Annotations for stops that landed.
 *
 * At most one per turn, which the server's one-row-per-turn constraint already
 * guarantees. A stop that expired - the turn ended before it reached a
 * boundary - is not rendered as a stop, because it did not stop anything.
 */
export function haltAnnotations(halts: Halt[]): TranscriptAnnotation[] {
  return halts
    .filter((halt) => halt.status === 'applied')
    .map((halt) => ({
      kind: 'halt' as const,
      id: halt.id,
      at: toTime(halt.applied_at ?? halt.created_at),
      mode: halt.mode,
      boundary: halt.applied_at_boundary ?? null,
      toolName: halt.applied_tool_name ?? null,
      ended: halt.turn_ended_at != null,
    }));
}

/**
 * Weave annotations into the message list by time.
 *
 * Placed after the last message that is not newer than the annotation, so a
 * nudge appears where it landed rather than at the bottom. An annotation with
 * no usable time, or one that predates everything on screen, goes at the end:
 * showing it late is a smaller lie than showing it before the conversation it
 * belongs to.
 */
export function weaveTranscript(
  messages: SessionMessage[],
  annotations: TranscriptAnnotation[]
): TranscriptItem[] {
  const items: TranscriptItem[] = messages.map((message) => ({
    kind: 'message' as const,
    key: `message-${message.index}`,
    message,
  }));

  if (annotations.length === 0) return items;

  const times = messages.map((message) => toTime(message.timestamp));
  const placed = new Map<number, TranscriptAnnotation[]>();
  const trailing: TranscriptAnnotation[] = [];

  for (const annotation of annotations) {
    if (annotation.at === null) {
      trailing.push(annotation);
      continue;
    }
    let position = -1;
    for (let index = 0; index < times.length; index += 1) {
      const time = times[index];
      if (time !== null && time <= annotation.at) position = index;
    }
    if (position === -1) {
      trailing.push(annotation);
      continue;
    }
    const bucket = placed.get(position) ?? [];
    bucket.push(annotation);
    placed.set(position, bucket);
  }

  const woven: TranscriptItem[] = [];
  items.forEach((item, index) => {
    woven.push(item);
    for (const annotation of placed.get(index) ?? []) {
      woven.push({
        kind: 'annotation',
        key: `${annotation.kind}-${annotation.id}`,
        annotation,
      });
    }
  });
  for (const annotation of trailing) {
    woven.push({
      kind: 'annotation',
      key: `${annotation.kind}-${annotation.id}`,
      annotation,
    });
  }
  return woven;
}
