import { Center, Loader } from '@mantine/core';
import { useRouter } from 'next/router';
import type { ReactElement } from 'react';

import { AppLayout } from '@/core/layouts/app-layout';
import TeamDetailPage from '@/core/page-components/teams/team-detail';
import type { NextPageWithLayout } from '@/core/types/page';

const TeamDetail: NextPageWithLayout = () => {
  const router = useRouter();

  // On the first client render of a dynamic route the query is still empty,
  // so the slug is read only once the router has hydrated it.
  if (!router.isReady) {
    return (
      <Center h={400}>
        <Loader size="lg" />
      </Center>
    );
  }

  const slug = typeof router.query.slug === 'string' ? router.query.slug : '';

  return <TeamDetailPage slug={slug} />;
};

TeamDetail.getLayout = (page: ReactElement) => {
  return <AppLayout>{page}</AppLayout>;
};

export default TeamDetail;
