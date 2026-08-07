import type { ReactElement } from 'react';

import { AppLayout } from '@/core/layouts/app-layout';
import KnowledgePanel from '@/core/page-components/knowledge/knowledge';
import type { NextPageWithLayout } from '@/core/types/page';

const Knowledge: NextPageWithLayout = () => {
  return <KnowledgePanel />;
};

Knowledge.getLayout = (page: ReactElement) => {
  return <AppLayout>{page}</AppLayout>;
};

export default Knowledge;
