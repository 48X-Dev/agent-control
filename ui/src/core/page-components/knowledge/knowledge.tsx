import {
  Alert,
  Button,
  Group,
  Loader,
  Stack,
  Tabs,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { IconAlertCircle, IconSearch } from '@tabler/icons-react';
import { useState } from 'react';

import { getErrorStatus, isForbiddenError } from '@/core/api/errors';
import {
  useKnowledgeRecent,
  useKnowledgeSearch,
  useKnowledgeStatus,
} from '@/core/hooks/query-hooks/use-company-knowledge';

import { FreshnessStripView, KnowledgeResults } from './knowledge-results';
import { KnowledgeSourcesView } from './knowledge-sources';

/**
 * The company-knowledge panel: the same corpus an agent reads, for a person.
 *
 * Two verbs and no third. There is no list, no cursor and no "load more",
 * which is the same refusal the agents' surface makes and for the same reason:
 * a paging control plus a patient operator is the whole corpus copied out one
 * page at a time, and the console is not the place to reopen a decision the
 * rest of the design is built on.
 *
 * It opens on "what changed" rather than an empty search box. A person
 * arriving here has usually not thought of a query yet, and the corpus block
 * that fills the freshness strip rides on any response - so opening on the
 * recency verb answers "is this mirror current" before anyone has to ask it.
 */

const SEARCH_TAB = 'search';
const RECENT_TAB = 'recent';
const SOURCES_TAB = 'sources';

/**
 * Something went wrong before the corpus was consulted, and which something
 * decides where the operator goes next.
 *
 * Three different first moves, so three sentences. A rejected credential is
 * fixed by signing in with a better key. A server that answered with an error
 * answered, so the fix is in its log, and "check that it is running" aimed at
 * a process that is running and returning 500s is the same wasted hour as the
 * reverse. Only a request that got no answer at all is a reachability problem.
 *
 * None of this covers the store being off or unreachable: those answer 200
 * with a refusal code, which is a sentence elsewhere on this page and never a
 * red box, because it is the corpus talking rather than a failure to reach it.
 * Nor 401 - the client's unauthorized hook replaces the page with the sign-in
 * gate before a lapsed session can reach here.
 */
function errorSentence(error: unknown, what: string): string {
  if (isForbiddenError(error)) {
    return `Your key was rejected, so the ${what} could not be loaded. This panel needs one carrying the company-knowledge status operation.`;
  }
  const status = getErrorStatus(error);
  if (status !== undefined && status >= 500) {
    return `The ${what} could not be loaded: the server answered with an error (status ${status}). Check the server log.`;
  }
  return `The ${what} could not be loaded: the server did not answer. Check that it is running and reachable from this console.`;
}

function RequestError({ error, what }: { error: unknown; what: string }) {
  return (
    <Alert
      color="red"
      icon={<IconAlertCircle size={16} />}
      data-testid="knowledge-error"
    >
      <Text size="sm">{errorSentence(error, what)}</Text>
    </Alert>
  );
}

const KnowledgePage = () => {
  const [draft, setDraft] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [tab, setTab] = useState<string | null>(RECENT_TAB);

  const search = useKnowledgeSearch(submitted);
  const recent = useKnowledgeRecent();
  const status = useKnowledgeStatus(tab === SOURCES_TAB);

  // Whichever view is in front owns the strip, so the footer describes the
  // response above it rather than whichever request happened to finish last.
  // It falls back to the other verb's response rather than disappearing: an
  // unrun search box is the commonest state of the Ask tab, and "how current
  // is this mirror" does not stop being worth answering because nobody has
  // typed a question yet.
  //
  // The whole query is carried, not just its data, because the strip needs two
  // more things from the same place: when this response arrived, so the age it
  // prints keeps counting, and how to ask again.
  const front = tab === SEARCH_TAB ? search : recent;
  const behind = tab === SEARCH_TAB ? recent : search;
  const showing = front.data ? front : behind.data ? behind : null;

  return (
    <Stack p="xl" maw={1100} mx="auto" my={0} gap="lg">
      <Stack gap={4}>
        <Title order={2} fw={600}>
          Company knowledge
        </Title>
        <Text size="sm" c="dimmed">
          A read-only mirror of the documents your agents can consult. Ask it
          the same way they do, and check what it has been told lately.
        </Text>
      </Stack>

      {/* The inactive panel unmounts rather than hiding. Corpus text is the
          one thing on this page nobody in the workspace authored, and leaving
          a second copy of it in the DOM behind `display: none` is a rendering
          nobody is looking at and no test is checking. */}
      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value={RECENT_TAB} data-testid="knowledge-tab-recent">
            What changed
          </Tabs.Tab>
          <Tabs.Tab value={SEARCH_TAB} data-testid="knowledge-tab-search">
            Ask
          </Tabs.Tab>
          <Tabs.Tab value={SOURCES_TAB} data-testid="knowledge-tab-sources">
            Sources
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value={RECENT_TAB} pt="md">
          <Stack gap="md">
            <Text size="sm" c="dimmed">
              The most recently changed documents in the mirror. One page, and
              there is no next one: this answers what moved, not what exists.
            </Text>
            {recent.isLoading ? (
              <Group gap="xs">
                <Loader size="xs" />
                <Text size="sm" c="dimmed">
                  Reading the mirror
                </Text>
              </Group>
            ) : recent.error ? (
              <RequestError error={recent.error} what="recent changes" />
            ) : (
              <KnowledgeResults
                response={recent.data}
                emptyMessage="Nothing has changed in the window this page asks about."
              />
            )}
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value={SEARCH_TAB} pt="md">
          <Stack gap="md">
            <form
              onSubmit={(event) => {
                event.preventDefault();
                setSubmitted(draft.trim());
              }}
            >
              <Group gap="xs" align="flex-end" wrap="nowrap">
                <TextInput
                  flex={1}
                  label="Ask the knowledge base"
                  placeholder="laptop reimbursement policy"
                  value={draft}
                  onChange={(event) => setDraft(event.currentTarget.value)}
                  data-testid="knowledge-search-input"
                />
                <Button
                  type="submit"
                  leftSection={<IconSearch size={16} />}
                  data-testid="knowledge-search-submit"
                >
                  Search
                </Button>
              </Group>
            </form>

            {/* Submitted on press rather than on keystroke: each call spends
                from a per-caller window, and search-as-you-type would spend it
                on prefixes of the question somebody meant to ask. */}
            {submitted.length === 0 ? (
              <Text size="sm" c="dimmed">
                Results are ranked, capped at one page, and quoted from the
                documents themselves.
              </Text>
            ) : search.isLoading ? (
              <Group gap="xs">
                <Loader size="xs" />
                <Text size="sm" c="dimmed">
                  Searching
                </Text>
              </Group>
            ) : search.error ? (
              <RequestError error={search.error} what="search" />
            ) : (
              <KnowledgeResults
                response={search.data}
                emptyMessage="Nothing in the mirror matched that. A gap is worth knowing about: the query ran and found nothing."
              />
            )}
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value={SOURCES_TAB} pt="md">
          <Stack gap="md">
            <Text size="sm" c="dimmed">
              What the mirror is built from, and whether each part of it still
              moves. Read-only.
            </Text>
            {status.isLoading ? (
              <Group gap="xs">
                <Loader size="xs" />
                <Text size="sm" c="dimmed">
                  Reading the sources
                </Text>
              </Group>
            ) : status.error ? (
              <RequestError error={status.error} what="source status" />
            ) : (
              <KnowledgeSourcesView
                status={status.data}
                dataUpdatedAt={status.dataUpdatedAt}
              />
            )}
          </Stack>
        </Tabs.Panel>
      </Tabs>

      {/* The sources view carries its own, richer version of this footer, so
          showing both would print the same age twice under one page. */}
      {tab === SOURCES_TAB ? null : (
        <FreshnessStripView
          response={showing?.data}
          dataUpdatedAt={showing?.dataUpdatedAt}
          onRecheck={showing ? () => void showing.refetch() : undefined}
          rechecking={showing?.isFetching}
        />
      )}
    </Stack>
  );
};

export default KnowledgePage;
