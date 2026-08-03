import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';

import type { ExpertTaxonomyRead } from '../types';
import ExpertClassificationDialog from './ExpertClassificationDialog';


const taxonomy: ExpertTaxonomyRead = {
  version: 1,
  categories: [
    { name: '工程研发', subcategories: ['AI 与智能体', '前端与客户端'] },
    { name: '市场营销', subcategories: ['内容与社交'] },
  ],
};

it('clears an invalid subcategory and submits a legal pair', async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  render(
    <ExpertClassificationDialog
      open
      expertCount={2}
      taxonomy={taxonomy}
      initialCategory="工程研发"
      initialSubcategory="前端与客户端"
      saving={false}
      onClose={vi.fn()}
      onSubmit={onSubmit}
    />,
  );
  expect(screen.getByText('将修改 2 位专家')).toBeInTheDocument();
  await user.click(screen.getByRole('combobox', { name: '一级分类' }));
  await user.click(screen.getByRole('option', { name: '市场营销' }));
  expect(screen.getByRole('button', { name: '保存分类' })).toBeDisabled();
  await user.click(screen.getByRole('combobox', { name: '二级分类' }));
  await user.click(screen.getByRole('option', { name: '内容与社交' }));
  await user.click(screen.getByRole('button', { name: '保存分类' }));
  expect(onSubmit).toHaveBeenCalledWith({
    category: '市场营销',
    subcategory: '内容与社交',
  });
});

it('disables saving while a request is in progress', () => {
  render(
    <ExpertClassificationDialog
      open
      expertCount={1}
      taxonomy={taxonomy}
      initialCategory="工程研发"
      initialSubcategory="AI 与智能体"
      saving
      onClose={vi.fn()}
      onSubmit={vi.fn()}
    />,
  );
  expect(screen.getByRole('button', { name: '保存中' })).toBeDisabled();
});
