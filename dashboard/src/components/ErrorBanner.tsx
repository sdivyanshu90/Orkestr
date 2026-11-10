import type { ReactElement } from 'react';

export interface BannerState {
  kind: 'error' | 'offline' | 'info';
  text: string;
}

interface ErrorBannerProps {
  banner: BannerState | null;
  onDismiss: () => void;
}

export default function ErrorBanner({ banner, onDismiss }: ErrorBannerProps): ReactElement | null {
  if (!banner) return null;
  return (
    <div className={`banner banner-${banner.kind}`} role="alert">
      <span className="banner-text">{banner.text}</span>
      <button type="button" className="banner-close" onClick={onDismiss}>
        dismiss
      </button>
    </div>
  );
}
