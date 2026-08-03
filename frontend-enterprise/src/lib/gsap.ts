import gsap from 'gsap';

/**
 * 用户开启「减弱动态效果」时跳过所有入场动画。
 * jsdom 没有 matchMedia，缺失时按「不减弱」处理。
 */
export function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export { gsap };
