import { Alert, Badge, Group, Stack, Text } from '@mantine/core';
import { IconAlertCircle, IconAlertTriangle } from '@tabler/icons-react';

import type { KnowledgeSourceStatus, KnowledgeStatus } from '@/core/api/types';

import {
  exclusionSummary,
  failureSentence,
  FALLBACK_STALENESS_WARN_SECONDS,
  formatDate,
  formatStaleness,
  sourceAgeSeconds,
  type SourceHealth,
  sourceHealth,
} from './formatting';
import classes from './knowledge.module.css';
import { useSecondsOnScreen } from './knowledge-results';

/**
 * What is configured to sync, and whether it still is.
 *
 * Three states get their own treatment rather than a cell in a table, because
 * all three read as success in a row of counts: a source indexing nothing, a
 * source whose credential stopped exchanging, and a schema the reader and the
 * sync disagree about. The last is corpus-wide, so it sits above the rows.
 *
 * Nothing here writes. There is no connect or re-auth control on purpose - see
 * the note at the foot of the panel, and section 16 of the plan.
 */

const KIND_LABELS: Record<string, string> = {
  drive: 'Google Drive',
  github: 'GitHub',
};

const HEALTH_CLASSES: Record<SourceHealth, string> = {
  disabled: classes.sourceDisabled,
  failing: classes.sourceFailing,
  no_documents: classes.sourceWarning,
  partial: classes.sourceWarning,
  stale: classes.sourceWarning,
  ok: '',
};

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? '' : 's'}`;
}

/**
 * Two different corpus-wide failures arrive in the same pair of fields, and
 * they send an operator to different places. A version number means the store
 * was read and is the wrong shape; no version means it was not read at all.
 */
function SchemaMismatch({ version }: { version: number | null }) {
  const unreadable = version === null;
  return (
    <Alert
      color="red"
      icon={<IconAlertCircle size={16} />}
      title={
        unreadable
          ? 'The corpus could not be read'
          : 'Nothing can be searched right now'
      }
      data-testid="knowledge-schema-mismatch"
    >
      <Text size="sm">
        {unreadable
          ? 'This server could not read the knowledge store at all, so every search is answering that the knowledge base is unavailable. Check the store is running and that this server was given its DSN.'
          : `The store is on schema version ${version}, which this server does not support, so every search is answering that the knowledge base is unavailable. Run the corpus migrations so the sync and the reader agree.`}{' '}
        Anything below is the last thing recorded, not a current reading.
      </Text>
    </Alert>
  );
}

function CorpusSummary({
  status,
  ageSeconds,
  warnAfterSeconds,
}: {
  status: KnowledgeStatus;
  ageSeconds: number | null;
  warnAfterSeconds: number;
}) {
  const age = formatStaleness(ageSeconds);
  const stale = ageSeconds !== null && ageSeconds > warnAfterSeconds;
  const failing = status.sources_failing ?? 0;

  return (
    <Group gap="xs" wrap="wrap" data-testid="knowledge-sources-summary">
      <Text size="sm">{plural(status.document_count ?? 0, 'document')}</Text>
      <Text size="sm" c="dimmed">
        · {plural(status.chunk_count ?? 0, 'chunk')}
      </Text>
      <Text size="sm" c={stale ? 'yellow.7' : 'dimmed'}>
        · {age ? `checked ${age} ago` : 'never checked'}
      </Text>
      {/* This counter includes an enabled source holding nothing, so it cannot
          be called a count of failed syncs without contradicting the rows. */}
      {failing > 0 ? (
        <Text size="sm" c="red.7">
          · {failing} of {status.sources?.length ?? failing}{' '}
          {failing === 1 ? 'needs' : 'need'} attention
        </Text>
      ) : null}
    </Group>
  );
}

function SourceRow({
  source,
  index,
  elapsedSeconds,
  warnAfterSeconds,
}: {
  source: KnowledgeSourceStatus;
  index: number;
  elapsedSeconds: number;
  warnAfterSeconds: number;
}) {
  const age = sourceAgeSeconds(source, elapsedSeconds);
  const health = sourceHealth(source, age, warnAfterSeconds);
  const interval = formatStaleness(age);
  const cursor = formatDate(source.cursor_advanced_at);
  const excluded = exclusionSummary(source.refusals_by_code);

  return (
    <Stack
      gap={6}
      className={`${classes.source} ${HEALTH_CLASSES[health]}`}
      data-testid={`knowledge-source-${index}`}
    >
      <Group gap="xs" justify="space-between" wrap="nowrap" align="flex-start">
        <Text size="sm" fw={600} className={classes.code}>
          {source.source_id}
        </Text>
        <Group gap={6} wrap="nowrap">
          <Badge size="xs" variant="light" color="gray">
            {KIND_LABELS[source.kind] ?? source.kind}
          </Badge>
          {source.enabled ? null : (
            <Badge
              size="xs"
              variant="light"
              color="gray"
              data-testid="knowledge-source-off"
            >
              Not syncing
            </Badge>
          )}
        </Group>
      </Group>

      <Text
        size="xs"
        c={health === 'stale' ? 'yellow.7' : 'dimmed'}
        data-testid="knowledge-source-meta"
      >
        {interval ? `synced ${interval} ago` : 'never synced'}
        {' · '}
        {source.document_count > 0
          ? plural(source.document_count, 'document')
          : 'nothing indexed'}
        {' · '}
        {cursor ? `cursor last moved ${cursor}` : 'cursor has never moved'}
      </Text>

      {health === 'failing' ? (
        <Alert
          color="red"
          icon={<IconAlertCircle size={16} />}
          data-testid="knowledge-source-failing"
        >
          <Text size="sm">
            This source stopped syncing.{' '}
            {failureSentence(source.last_failure_code)}
          </Text>
          {source.last_failure_code ? (
            <Text size="xs" c="dimmed" className={classes.code} mt={4}>
              {source.last_failure_code}
            </Text>
          ) : null}
        </Alert>
      ) : null}

      {health === 'no_documents' ? (
        <Alert
          color="yellow"
          icon={<IconAlertTriangle size={16} />}
          data-testid="knowledge-source-empty"
        >
          <Text size="sm">
            Enabled, and holding nothing. An empty folder and a source whose id
            or credential never reached the sync look identical from here, so
            check the sync&apos;s environment before concluding the folder is
            empty.
          </Text>
        </Alert>
      ) : null}

      {health === 'partial' ? (
        <Alert
          color="yellow"
          icon={<IconAlertTriangle size={16} />}
          data-testid="knowledge-source-partial"
        >
          <Text size="sm">
            The last run finished but recorded an error, so this source is
            indexed and may be incomplete.{' '}
            {failureSentence(source.last_failure_code)}
          </Text>
          <Text size="xs" c="dimmed" className={classes.code} mt={4}>
            {source.last_failure_code}
          </Text>
        </Alert>
      ) : null}

      {health === 'stale' ? (
        <Alert
          color="yellow"
          icon={<IconAlertTriangle size={16} />}
          data-testid="knowledge-source-stale"
        >
          <Text size="sm">
            Behind: last verified {interval} ago, past this deployment&apos;s{' '}
            {formatStaleness(warnAfterSeconds)} threshold. Answers drawn from it
            may be missing recent changes.
          </Text>
        </Alert>
      ) : null}

      {/* A standing total of what is out of the corpus today, not a tally from
          the last run: these are the tombstone reasons on this source's rows. */}
      {excluded ? (
        <Text size="xs" c="dimmed" data-testid="knowledge-source-excluded">
          Currently excluded: {excluded}
        </Text>
      ) : null}
    </Stack>
  );
}

export function KnowledgeSourcesView({
  status,
  dataUpdatedAt,
}: {
  status: KnowledgeStatus | undefined;
  dataUpdatedAt?: number;
}) {
  const elapsed = useSecondsOnScreen(status ? dataUpdatedAt : undefined);
  if (!status) return null;

  // Cast, not validated, the same guard the freshness strip takes: a malformed
  // 200 from a proxy should cost the rows rather than the page.
  const sources = status.sources ?? [];
  const unreadable = status.schema_supported === false;
  const warnAfter =
    status.staleness_warn_seconds ?? FALLBACK_STALENESS_WARN_SECONDS;
  const corpusAge =
    typeof status.stale_seconds === 'number'
      ? status.stale_seconds + elapsed
      : null;

  return (
    <Stack gap="md" data-testid="knowledge-sources">
      {unreadable ? (
        <SchemaMismatch version={status.schema_version ?? null} />
      ) : null}

      <CorpusSummary
        status={status}
        ageSeconds={corpusAge}
        warnAfterSeconds={warnAfter}
      />

      {/* Silent when the store could not be read: an empty list then means the
          rows could not be fetched, not that nobody has configured one. */}
      {sources.length === 0 ? (
        unreadable ? null : (
          <Text size="sm" c="dimmed" data-testid="knowledge-sources-none">
            No source has been configured yet. The sync names its own in a
            config file and records them on its first run, so this stays empty
            until one has run.
          </Text>
        )
      ) : (
        sources.map((source, index) => (
          <SourceRow
            key={`${source.source_id}-${index}`}
            source={source}
            index={index}
            elapsedSeconds={elapsed}
            warnAfterSeconds={warnAfter}
          />
        ))
      )}

      <Text size="xs" c="dimmed" data-testid="knowledge-sources-readonly">
        This panel only reads. Linking a source is a one-time job at the command
        line, and there is deliberately no button for it here: it would move a
        live source credential into the database, where every backup carries it.
      </Text>
    </Stack>
  );
}
