import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { BRAND_STORAGE_KEYS } from '@/lib/brand-storage';
import { PRODUCT_EVENTS } from '@/lib/product-events';

import OnboardingGuide from './OnboardingGuide';

describe('OnboardingGuide', () => {
  it('用共格·序伴的业务闭环介绍产品，并在完成后记录引导状态', async () => {
    const user = userEvent.setup();
    const completed = vi.fn();
    window.addEventListener(PRODUCT_EVENTS.onboardingCompleted, completed);

    render(<OnboardingGuide />);

    expect(await screen.findByRole('dialog')).toHaveTextContent('让企业经验进入工作');
    expect(screen.getByText('知识有依据')).toBeInTheDocument();
    expect(screen.getByText('流程能执行')).toBeInTheDocument();
    expect(screen.getByText('结果可复盘')).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '下一步' }));

    expect(screen.getByRole('dialog')).toHaveTextContent('一条闭环，带数字员工真正上岗');
    expect(screen.getByText('定义岗位')).toBeInTheDocument();
    expect(screen.getByText('连接能力')).toBeInTheDocument();
    expect(screen.getByText('交付与沉淀')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '开始使用' }));

    expect(window.localStorage.getItem(BRAND_STORAGE_KEYS.onboardingSeen)).toBe('1');
    expect(completed).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    window.removeEventListener(PRODUCT_EVENTS.onboardingCompleted, completed);
  });
});
