import type { CSSProperties } from 'react';
import { CircleCheckIcon, InfoIcon, Loader2Icon, OctagonXIcon, TriangleAlertIcon } from 'lucide-react';
import { Toaster as Sonner, type ToasterProps } from 'sonner';

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      theme="light"
      className="toaster group"
      icons={{
        success: <CircleCheckIcon className="size-4" />,
        info: <InfoIcon className="size-4" />,
        warning: <TriangleAlertIcon className="size-4" />,
        error: <OctagonXIcon className="size-4" />,
        loading: <Loader2Icon className="size-4 animate-spin" />,
      }}
      style={
        {
          '--normal-bg': 'var(--gg-paper)',
          '--normal-text': 'var(--gg-ink)',
          '--normal-border': 'var(--gg-border)',
          '--border-radius': 'var(--gg-radius-control)',
        } as CSSProperties
      }
      toastOptions={{
        classNames: {
          toast: 'cn-toast gg-type-body gg-typography-scope',
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
