import type { ReactElement } from 'react';

import { AppLayout } from '@/core/layouts/app-layout';
import TeamsPage from '@/core/page-components/teams/teams';
import type { NextPageWithLayout } from '@/core/types/page';

const Teams: NextPageWithLayout = () => {
  return <TeamsPage />;
};

Teams.getLayout = (page: ReactElement) => {
  return <AppLayout>{page}</AppLayout>;
};

export default Teams;
