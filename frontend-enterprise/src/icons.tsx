import type { CSSProperties } from 'react';
import ProductIcon, { type ProductIconName } from './components/ProductIcon';

type IconProps = {
  className?: string;
  rotate?: number;
  spin?: boolean;
  style?: CSSProperties;
  [key: string]: unknown;
};

function GonggeAntIcon({ name, rotate = 0, spin = false, className = '', style }: IconProps & { name: ProductIconName }) {
  const transform = [style?.transform, rotate ? `rotate(${rotate}deg)` : ''].filter(Boolean).join(' ');
  return (
    <ProductIcon
      name={name}
      className={`${spin ? 'product-icon-spin ' : ''}${className}`.trim()}
      style={{ ...style, transform: transform || undefined }}
    />
  );
}

export const ApiOutlined = (props: IconProps) => <GonggeAntIcon name="model" {...props} />;
export const AppstoreOutlined = (props: IconProps) => <GonggeAntIcon name="grid" {...props} />;
export const ArrowLeftOutlined = (props: IconProps) => <GonggeAntIcon name="arrow" rotate={180} {...props} />;
export const AuditOutlined = (props: IconProps) => <GonggeAntIcon name="file" {...props} />;
export const BranchesOutlined = (props: IconProps) => <GonggeAntIcon name="branch" {...props} />;
export const CheckCircleFilled = (props: IconProps) => <GonggeAntIcon name="check" {...props} />;
export const CheckCircleOutlined = (props: IconProps) => <GonggeAntIcon name="check" {...props} />;
export const CheckOutlined = (props: IconProps) => <GonggeAntIcon name="check" {...props} />;
export const ClockCircleOutlined = (props: IconProps) => <GonggeAntIcon name="clock" {...props} />;
export const CloseCircleOutlined = (props: IconProps) => <GonggeAntIcon name="close" {...props} />;
export const CloseOutlined = (props: IconProps) => <GonggeAntIcon name="close" {...props} />;
export const CloudOutlined = (props: IconProps) => <GonggeAntIcon name="cloud" {...props} />;
export const CloudSyncOutlined = (props: IconProps) => <GonggeAntIcon name="refresh" {...props} />;
export const CodeOutlined = (props: IconProps) => <GonggeAntIcon name="code" {...props} />;
export const DatabaseOutlined = (props: IconProps) => <GonggeAntIcon name="database" {...props} />;
export const DeleteOutlined = (props: IconProps) => <GonggeAntIcon name="trash" {...props} />;
export const DesktopOutlined = (props: IconProps) => <GonggeAntIcon name="desktop" {...props} />;
export const DownOutlined = (props: IconProps) => <GonggeAntIcon name="arrow" rotate={90} {...props} />;
export const DownloadOutlined = (props: IconProps) => <GonggeAntIcon name="download" {...props} />;
export const EditOutlined = (props: IconProps) => <GonggeAntIcon name="edit" {...props} />;
export const ExperimentOutlined = (props: IconProps) => <GonggeAntIcon name="tool" {...props} />;
export const EyeOutlined = (props: IconProps) => <GonggeAntIcon name="eye" {...props} />;
export const FileAddOutlined = (props: IconProps) => <GonggeAntIcon name="plus" {...props} />;
export const FileMarkdownOutlined = (props: IconProps) => <GonggeAntIcon name="file" {...props} />;
export const FileSearchOutlined = (props: IconProps) => <GonggeAntIcon name="file" {...props} />;
export const FileTextOutlined = (props: IconProps) => <GonggeAntIcon name="file" {...props} />;
export const FolderOpenOutlined = (props: IconProps) => <GonggeAntIcon name="folder" {...props} />;
export const GithubOutlined = (props: IconProps) => <GonggeAntIcon name="code" {...props} />;
export const HistoryOutlined = (props: IconProps) => <GonggeAntIcon name="history" {...props} />;
export const IdcardOutlined = (props: IconProps) => <GonggeAntIcon name="user" {...props} />;
export const InboxOutlined = (props: IconProps) => <GonggeAntIcon name="inbox" {...props} />;
export const InfoCircleOutlined = (props: IconProps) => <GonggeAntIcon name="info" {...props} />;
export const LoadingOutlined = (props: IconProps) => <GonggeAntIcon name="refresh" spin {...props} />;
export const LockOutlined = (props: IconProps) => <GonggeAntIcon name="lock" {...props} />;
export const MessageOutlined = (props: IconProps) => <GonggeAntIcon name="chat" {...props} />;
export const MoonOutlined = (props: IconProps) => <GonggeAntIcon name="moon" {...props} />;
export const MoreOutlined = (props: IconProps) => <GonggeAntIcon name="more" {...props} />;
export const PauseCircleOutlined = (props: IconProps) => <GonggeAntIcon name="pause" {...props} />;
export const PlayCircleOutlined = (props: IconProps) => <GonggeAntIcon name="play" {...props} />;
export const PlusOutlined = (props: IconProps) => <GonggeAntIcon name="plus" {...props} />;
export const ProfileOutlined = (props: IconProps) => <GonggeAntIcon name="filter" {...props} />;
export const ReloadOutlined = (props: IconProps) => <GonggeAntIcon name="refresh" {...props} />;
export const RightOutlined = (props: IconProps) => <GonggeAntIcon name="arrow" {...props} />;
export const RollbackOutlined = (props: IconProps) => <GonggeAntIcon name="history" {...props} />;
export const SaveOutlined = (props: IconProps) => <GonggeAntIcon name="save" {...props} />;
export const SearchOutlined = (props: IconProps) => <GonggeAntIcon name="search" {...props} />;
export const SendOutlined = (props: IconProps) => <GonggeAntIcon name="send" {...props} />;
export const SolutionOutlined = (props: IconProps) => <GonggeAntIcon name="spark" {...props} />;
export const StopOutlined = (props: IconProps) => <GonggeAntIcon name="stop" {...props} />;
export const SunOutlined = (props: IconProps) => <GonggeAntIcon name="sun" {...props} />;
export const SyncOutlined = (props: IconProps) => <GonggeAntIcon name="refresh" {...props} />;
export const TeamOutlined = (props: IconProps) => <GonggeAntIcon name="user" {...props} />;
export const ToolOutlined = (props: IconProps) => <GonggeAntIcon name="tool" {...props} />;
export const UploadOutlined = (props: IconProps) => <GonggeAntIcon name="upload" {...props} />;
export const UserOutlined = (props: IconProps) => <GonggeAntIcon name="user" {...props} />;
export const UsergroupAddOutlined = (props: IconProps) => <GonggeAntIcon name="user" {...props} />;
export const WarningOutlined = (props: IconProps) => <GonggeAntIcon name="warning" {...props} />;
