import {
  AlertTriangle,
  CalendarClock,
  FileCode2,
  FolderOpen,
  HelpCircle,
  History,
  Home,
  Info,
  KeyRound,
  Layers,
  ListChecks,
  type LucideIcon,
  MessageCircle,
  Mic,
  PlugZap,
  Search,
  ShieldCheck,
  StickyNote,
  Wrench,
  Zap,
} from 'lucide-react';

export interface NavItem {
  title: string;
  to: string;
  icon: LucideIcon;
}

export interface NavSection {
  /** Omitted for the leading, unlabelled group of primary destinations. */
  title?: string;
  items: NavItem[];
}

/**
 * Every page reachable from the main navigation. The desktop sidebar and the
 * mobile drawer both render this list, so a page added here appears in both.
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    items: [
      { title: 'Home', to: '/', icon: Home },
      { title: 'Chat', to: '/chat', icon: MessageCircle },
      { title: 'Voice', to: '/voice', icon: Mic },
      { title: 'History', to: '/history', icon: History },
    ],
  },
  {
    title: 'Knowledge',
    items: [
      { title: 'Notes', to: '/notes', icon: StickyNote },
      { title: 'Documents', to: '/documents', icon: FolderOpen },
      { title: 'Search', to: '/vector-search', icon: Search },
      { title: 'Context', to: '/context', icon: Layers },
    ],
  },
  {
    title: 'Automation',
    items: [
      { title: 'Automations', to: '/automations', icon: Zap },
      { title: 'Events', to: '/events', icon: CalendarClock },
      { title: 'Scripts', to: '/scripts', icon: FileCode2 },
      { title: 'Artifact review', to: '/artifacts', icon: ShieldCheck },
    ],
  },
  {
    title: 'System',
    items: [
      { title: 'Tools', to: '/tools', icon: Wrench },
      { title: 'Task queue', to: '/tasks', icon: ListChecks },
      { title: 'Error logs', to: '/errors', icon: AlertTriangle },
    ],
  },
  {
    title: 'Settings',
    items: [
      { title: 'Connected accounts', to: '/settings/accounts', icon: PlugZap },
      { title: 'API tokens', to: '/settings/tokens', icon: KeyRound },
    ],
  },
];

/** Secondary destinations pinned to the bottom of the navigation. */
export const NAV_FOOTER_ITEMS: NavItem[] = [
  { title: 'Help', to: '/docs', icon: HelpCircle },
  { title: 'About', to: '/about', icon: Info },
];

const ALL_NAV_ITEMS: NavItem[] = [
  ...NAV_SECTIONS.flatMap((section) => section.items),
  ...NAV_FOOTER_ITEMS,
];

/** Whether `pathname` is `item`'s page or one nested beneath it. */
export function isNavItemActive(item: NavItem, pathname: string): boolean {
  const path = pathname.replace(/\/+$/, '') || '/';
  if (item.to === '/') {
    return path === '/';
  }
  return path === item.to || path.startsWith(`${item.to}/`);
}

/** The navigation entry for the page at `pathname`, if it has one. */
export function findActiveNavItem(pathname: string): NavItem | undefined {
  return ALL_NAV_ITEMS.find((item) => isNavItemActive(item, pathname));
}
