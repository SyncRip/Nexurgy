// Dev-only gallery of the components/ui primitives (route /dev/ui, registered
// only when import.meta.env.DEV). Every primitive in its main states on one
// page, with switches for theme, language and reduced motion, so the skins can
// be checked in both themes without hunting for a screen that uses them.
// Copy is not translated: this page never ships.

import { Background, ReactFlow, type Edge, type Node } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Copy, MoreHorizontal, Pencil, Plus, Settings2, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, IconButton } from '../../components/ui/Button';
import { Card, Panel } from '../../components/ui/Card';
import { ContextMenuArea } from '../../components/ui/ContextMenu';
import { ConfirmDialog, Dialog } from '../../components/ui/Dialog';
import { Drawer } from '../../components/ui/Drawer';
import { Field } from '../../components/ui/Form';
import { Kbd } from '../../components/ui/Kbd';
import { Menu, type MenuItem } from '../../components/ui/Menu';
import { NumberField } from '../../components/ui/NumberField';
import { Popover } from '../../components/ui/Popover';
import { Select } from '../../components/ui/Select';
import { EmptyState, SkeletonRows, Spinner, type EmptyKind } from '../../components/ui/States';
import { StatusChip, type StatusKind } from '../../components/ui/StatusChip';
import { SegmentedControl, Tabs } from '../../components/ui/Tabs';
import { useToast, type ToastTone } from '../../components/ui/Toast';
import { Tooltip, TooltipProvider } from '../../components/ui/Tooltip';
import { LANGS } from '../../i18n/core';
import { useI18n } from '../../i18n/I18nProvider';
import { THEME_PREFS, useTheme } from '../../prefs/ThemeProvider';
import './UiPlayground.css';

const VARIANTS = ['primary', 'secondary', 'ghost', 'danger'] as const;
const SIZES = ['sm', 'md', 'lg'] as const;
const STATUSES: readonly StatusKind[] = ['ok', 'warn', 'err', 'running', 'pending', 'stale', 'info'];
const EMPTY_KINDS: readonly EmptyKind[] = ['empty', 'no-result', 'no-permission', 'disabled', 'error'];
const TOAST_TONES: readonly ToastTone[] = ['ok', 'info', 'err'];
const MATERIALS = [
  { value: 'al6061', label: 'Aluminium 6061' },
  { value: 'ss304', label: 'Stainless 304' },
  { value: 'pom', label: 'POM (disabled)', disabled: true },
];
/** How long the busy demos hold their overlay open. */
const BUSY_MS = 1500;
const noop = () => {};

type Motion = 'full' | 'reduced';

function PreferenceBar() {
  const { pref, setPref } = useTheme();
  const { lang, setLang } = useI18n();
  const [motion, setMotion] = useState<Motion>(() => (document.documentElement.dataset.motion === 'reduced' ? 'reduced' : 'full'));

  // Nothing in the app sets data-motion yet; this switch exercises the reduced-motion rules.
  useEffect(() => {
    const root = document.documentElement;
    if (motion === 'reduced') root.dataset.motion = 'reduced';
    return () => {
      delete root.dataset.motion;
    };
  }, [motion]);

  return (
    <div className="dev-ui__row">
      <SegmentedControl aria-label="Theme" items={THEME_PREFS.map((p) => ({ value: p, label: p }))} value={pref} onChange={setPref} />
      <SegmentedControl aria-label="Language" items={LANGS.map((l) => ({ value: l, label: l }))} value={lang} onChange={setLang} />
      <SegmentedControl
        aria-label="Motion"
        items={[
          { value: 'full', label: 'motion' },
          { value: 'reduced', label: 'reduced' },
        ]}
        value={motion}
        onChange={setMotion}
      />
    </div>
  );
}

function ButtonsDemo() {
  const [loading, setLoading] = useState(false);
  const save = () => {
    setLoading(true);
    window.setTimeout(() => setLoading(false), BUSY_MS);
  };
  return (
    <Panel title="Button / IconButton / Tooltip">
      {SIZES.map((size) => (
        <div className="dev-ui__row" key={size}>
          {VARIANTS.map((variant) => (
            <Button key={variant} variant={variant} size={size}>
              {`${variant} ${size}`}
            </Button>
          ))}
          <IconButton size={size} label={`Settings (${size})`} icon={<Settings2 />} />
        </div>
      ))}
      <div className="dev-ui__row">
        <Button variant="primary" icon={<Plus />}>
          With icon
        </Button>
        <Button variant="primary" loading={loading} onClick={save}>
          Save (loading)
        </Button>
        <Button disabled>Disabled</Button>
        <Tooltip content="Edit name">
          <IconButton label="Edit" icon={<Pencil />} />
        </Tooltip>
        <Tooltip content={<>Search <Kbd keys={['Mod', 'K']} /></>} side="bottom">
          <Button variant="ghost">Tooltip below</Button>
        </Tooltip>
      </div>
    </Panel>
  );
}

function NavigationDemo() {
  const [tab, setTab] = useState('overview');
  const [pill, setPill] = useState('2d');
  const [view, setView] = useState('grid');
  return (
    <Panel title="Tabs / SegmentedControl">
      <Tabs
        aria-label="Template sections"
        value={tab}
        onChange={setTab}
        items={[
          { value: 'overview', label: 'Overview', panel: <p className="dev-ui__note">Overview panel</p> },
          { value: 'runs', label: 'Runs', meta: 12, panel: <p className="dev-ui__note">Runs panel</p> },
          { value: 'archived', label: 'Archived', disabled: true, panel: null },
        ]}
      />
      <div className="dev-ui__row">
        <Tabs
          aria-label="Drawing view"
          variant="pill"
          value={pill}
          onChange={setPill}
          items={[
            { value: '2d', label: '2D' },
            { value: '3d', label: '3D' },
          ]}
        />
        <SegmentedControl
          aria-label="Layout"
          size="md"
          value={view}
          onChange={setView}
          items={[
            { value: 'grid', label: 'Grid' },
            { value: 'list', label: 'List' },
            { value: 'board', label: 'Board', disabled: true },
          ]}
        />
      </div>
    </Panel>
  );
}

function FormDemo() {
  const [material, setMaterial] = useState<string | null>(null);
  const [quantity, setQuantity] = useState<number | null>(10);
  const [thickness, setThickness] = useState<number | null>(2.5);
  return (
    <Panel title="Field / Select / NumberField">
      <div className="dev-ui__grid">
        <Field label="Material" hint="Placeholder until chosen">
          <Select options={MATERIALS} value={material} onChange={setMaterial} />
        </Field>
        <Field label="Material (error)" error="Pick a material">
          <Select options={MATERIALS} value={null} onChange={setMaterial} />
        </Field>
        <Field label="Disabled">
          <Select options={MATERIALS} value="al6061" onChange={setMaterial} disabled />
        </Field>
        <Field label="Quantity" hint="1–999">
          <NumberField value={quantity} onChange={setQuantity} min={1} max={999} />
        </Field>
        <Field label="Thickness" optional>
          <NumberField value={thickness} onChange={setThickness} step={0.1} format={{ maximumFractionDigits: 2 }} unit="mm" />
        </Field>
        <Field label="Compact, no steppers">
          <NumberField value={quantity} onChange={setQuantity} size="sm" hideSteppers />
        </Field>
      </div>
    </Panel>
  );
}

type Overlay = 'dialog' | 'confirm' | 'drawer-md' | 'drawer-lg' | null;

function OverlaysDemo() {
  const toast = useToast();
  const [open, setOpen] = useState<Overlay>(null);
  const [busy, setBusy] = useState(false);
  const close = () => setOpen(null);
  // Holds the overlay open for a moment: Esc / backdrop must not close it while busy.
  const finishSlowly = (message: string) => {
    setBusy(true);
    window.setTimeout(() => {
      setBusy(false);
      setOpen(null);
      toast(message, { tone: 'ok' });
    }, BUSY_MS);
  };
  const menuItems: MenuItem[] = [
    { id: 'rename', label: 'Rename', icon: <Pencil />, onSelect: () => toast('Rename') },
    { id: 'copy', label: 'Duplicate', icon: <Copy />, onSelect: () => toast('Duplicate') },
    { id: 'off', label: 'Disabled item', disabled: true, onSelect: noop },
    { id: 'delete', label: 'Delete', icon: <Trash2 />, danger: true, separator: true, onSelect: () => setOpen('confirm') },
  ];
  const drawerSize = open === 'drawer-lg' ? 'lg' : 'md';

  return (
    <Panel title="Dialog / Drawer / Menu / Popover / Toast">
      <div className="dev-ui__row">
        <Button onClick={() => setOpen('dialog')}>Dialog</Button>
        <Button variant="danger" onClick={() => setOpen('confirm')}>
          ConfirmDialog
        </Button>
        <Button onClick={() => setOpen('drawer-md')}>Drawer md</Button>
        <Button onClick={() => setOpen('drawer-lg')}>Drawer lg</Button>
        <Menu label="More actions" trigger={<MoreHorizontal aria-hidden />} items={menuItems} header="Part P-104" />
        <Popover label="Filters" trigger="Popover" align="start">
          {(closePopover) => (
            <div className="dev-ui__stack">
              <p className="dev-ui__note">Disclosure content; focus stays on the trigger.</p>
              <Button size="sm" onClick={closePopover}>
                Close
              </Button>
            </div>
          )}
        </Popover>
      </div>
      <div className="dev-ui__row">
        {TOAST_TONES.map((tone) => (
          <Button key={tone} variant="ghost" onClick={() => toast(`Toast: ${tone}`, { tone })}>
            {`Toast ${tone}`}
          </Button>
        ))}
        <Button variant="ghost" onClick={() => toast('Run archived', { action: { label: 'Undo', onClick: () => toast('Restored', { tone: 'ok' }) } })}>
          Toast with action
        </Button>
      </div>

      {open === 'dialog' && (
        <Dialog
          title="Rename part"
          subtitle="Select inside a dialog must open above it"
          onClose={close}
          busy={busy}
          footer={
            <>
              <Button variant="ghost" onClick={close} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" loading={busy} onClick={() => finishSlowly('Renamed')}>
                Save
              </Button>
            </>
          }
        >
          <div className="dev-ui__stack">
            <Field label="Name">
              <input className="input" defaultValue="Bracket" data-autofocus />
            </Field>
            <Field label="Material">
              <Select options={MATERIALS} value="al6061" onChange={noop} />
            </Field>
          </div>
        </Dialog>
      )}
      {open === 'confirm' && (
        <ConfirmDialog
          title="Delete part?"
          body="The part and its drawings move to the archive."
          confirmLabel="Delete"
          danger
          busy={busy}
          onConfirm={() => finishSlowly('Deleted')}
          onClose={close}
        />
      )}
      {(open === 'drawer-md' || open === 'drawer-lg') && (
        <Drawer
          title="Part details"
          subtitle={`Drawer ${drawerSize}: right panel ≥1024px, bottom sheet below`}
          size={drawerSize}
          busy={busy}
          onClose={close}
          footer={
            <>
              <Button variant="ghost" onClick={close} disabled={busy}>
                Close
              </Button>
              <Button variant="primary" loading={busy} onClick={() => finishSlowly('Saved')}>
                Save
              </Button>
            </>
          }
        >
          <div className="dev-ui__stack">
            <Field label="Material">
              <Select options={MATERIALS} value="ss304" onChange={noop} />
            </Field>
            <SkeletonRows rows={4} cols={3} />
          </div>
        </Drawer>
      )}
    </Panel>
  );
}

function CardsDemo() {
  const [selected, setSelected] = useState(0);
  const cards: { name: string; status: StatusKind }[] = [
    { name: 'Bracket', status: 'ok' },
    { name: 'Housing', status: 'warn' },
    { name: 'Shaft', status: 'err' },
  ];
  return (
    <Panel title="Card / Panel">
      <div className="dev-ui__grid">
        <Card>Static card</Card>
        {cards.map((card, i) => (
          <Card key={card.name} interactive selected={selected === i} status={card.status} render={<button type="button" aria-pressed={selected === i} />} onClick={() => setSelected(i)}>
            <div className="dev-ui__stack">
              <strong>{card.name}</strong>
              <StatusChip status={card.status} label={card.status} />
            </div>
          </Card>
        ))}
      </div>
      <Panel title="Collapsible panel" level={3} collapsible defaultOpen actions={<Button size="sm" variant="ghost">Action</Button>}>
        <p className="dev-ui__note">The body stays mounted while collapsed, so drafts survive.</p>
      </Panel>
    </Panel>
  );
}

function FeedbackDemo() {
  return (
    <>
      <Panel title="StatusChip / Kbd / Spinner">
        <div className="dev-ui__row">
          {STATUSES.map((status) => (
            <StatusChip key={status} status={status} label={status} />
          ))}
        </div>
        <div className="dev-ui__row">
          <Kbd keys={['Mod', 'K']} />
          <Kbd keys="Esc" />
          <Kbd keys={['Mod', 'Shift', 'P']} mac />
          <Kbd keys={['Mod', 'Shift', 'P']} mac={false} />
          <Spinner label="Loading" />
        </div>
      </Panel>
      <Panel title="EmptyState kinds">
        <div className="dev-ui__grid">
          {EMPTY_KINDS.map((kind) => (
            <Card key={kind} padding="sm">
              <EmptyState kind={kind} compact onRetry={kind === 'error' ? noop : undefined} onClearFilters={kind === 'no-result' ? noop : undefined} />
            </Card>
          ))}
        </div>
      </Panel>
    </>
  );
}

type CanvasTarget = { kind: 'node'; id: string } | { kind: 'edge'; id: string } | { kind: 'pane' };

const DEMO_NODES: Node[] = [
  { id: 'cut', position: { x: 0, y: 60 }, data: { label: 'Laser cut' } },
  { id: 'bend', position: { x: 240, y: 0 }, data: { label: 'Bend' } },
  { id: 'paint', position: { x: 240, y: 130 }, data: { label: 'Paint' } },
];
const DEMO_EDGES: Edge[] = [
  { id: 'cut-bend', source: 'cut', target: 'bend' },
  { id: 'cut-paint', source: 'cut', target: 'paint' },
];

function resolveCanvasTarget(el: Element): CanvasTarget | null {
  const node = el.closest<HTMLElement>('.react-flow__node');
  if (node?.dataset.id) return { kind: 'node', id: node.dataset.id };
  const edge = el.closest<SVGElement>('.react-flow__edge');
  if (edge?.dataset.id) return { kind: 'edge', id: edge.dataset.id };
  return el.closest('.react-flow__pane') ? { kind: 'pane' } : null;
}

function CanvasMenuDemo() {
  const toast = useToast();
  const { resolved } = useTheme();
  const items = (target: CanvasTarget): MenuItem[] => {
    if (target.kind === 'pane') return [{ id: 'add', label: 'Add node here', icon: <Plus />, onSelect: () => toast('Add node') }];
    if (target.kind === 'edge') return [{ id: 'del', label: 'Delete connection', icon: <Trash2 />, danger: true, onSelect: () => toast(`Delete ${target.id}`) }];
    return [
      { id: 'dup', label: `Duplicate ${target.id}`, icon: <Copy />, onSelect: () => toast(`Duplicate ${target.id}`) },
      { id: 'del', label: 'Delete', icon: <Trash2 />, danger: true, separator: true, onSelect: () => toast(`Delete ${target.id}`) },
    ];
  };
  return (
    <Panel title="ContextMenuArea on React Flow (P0 spike)">
      <p className="dev-ui__note">
        Right-click a node, a connection or the empty canvas; <Kbd keys={['Shift', 'F10']} /> on a focused node; long-press on touch. Delete inside the menu must not delete the node.
      </p>
      <ContextMenuArea label="Canvas actions" resolveTarget={resolveCanvasTarget} items={items} className="dev-ui__canvas">
        <ReactFlow defaultNodes={DEMO_NODES} defaultEdges={DEMO_EDGES} deleteKeyCode={['Backspace', 'Delete']} colorMode={resolved} fitView>
          <Background />
        </ReactFlow>
      </ContextMenuArea>
    </Panel>
  );
}

export default function UiPlayground() {
  return (
    <TooltipProvider>
      <div className="page dev-ui">
        <header className="page-head">
          <div className="page-titles">
            <span className="eyebrow-sm">Dev only · /dev/ui</span>
            <h1>UI primitives</h1>
          </div>
          <PreferenceBar />
        </header>
        <ButtonsDemo />
        <NavigationDemo />
        <FormDemo />
        <OverlaysDemo />
        <CardsDemo />
        <FeedbackDemo />
        <CanvasMenuDemo />
      </div>
    </TooltipProvider>
  );
}
