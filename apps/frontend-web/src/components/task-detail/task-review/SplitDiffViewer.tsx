import { useMemo } from 'react';
import { ScrollArea } from '../../ui/scroll-area';
import { cn } from '../../../lib/utils';

export interface SplitDiffViewerProps {
  /** The raw git diff content string for a single file */
  diff: string;
  /** Maximum height of the diff viewer (CSS value). Default: "400px" */
  maxHeight?: string;
  /** Additional className for the container */
  className?: string;
}

type RowType = 'context' | 'modified' | 'deletion' | 'addition' | 'hunk';

interface DiffCell {
  /** 1-based line number in the corresponding file, or null for filler cells */
  lineNumber: number | null;
  /** Line content with the leading +/-/space marker stripped */
  content: string;
}

interface SplitRow {
  type: RowType;
  left: DiffCell | null;
  right: DiffCell | null;
  /** Raw text for hunk header rows (@@ ... @@) */
  hunkText?: string;
}

const HUNK_HEADER_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/**
 * Pair up a contiguous block of deletions and additions into aligned rows.
 * Deletions land on the left, additions on the right; where both exist on the
 * same index the row is a "modified" line, otherwise a pure deletion/addition.
 */
function flushBlock(
  deletions: DiffCell[],
  additions: DiffCell[],
  rows: SplitRow[]
): void {
  const count = Math.max(deletions.length, additions.length);
  for (let i = 0; i < count; i++) {
    const left = deletions[i] ?? null;
    const right = additions[i] ?? null;
    let type: RowType;
    if (left && right) type = 'modified';
    else if (left) type = 'deletion';
    else type = 'addition';
    rows.push({ type, left, right });
  }
  deletions.length = 0;
  additions.length = 0;
}

/**
 * Parse a raw git diff for a single file into aligned side-by-side rows.
 * Lines before the first hunk header (diff/index/--- /+++) are ignored.
 */
function parseSplitDiff(diff: string): SplitRow[] {
  const lines = diff.split('\n');
  const rows: SplitRow[] = [];
  const deletions: DiffCell[] = [];
  const additions: DiffCell[] = [];

  let oldLn = 0;
  let newLn = 0;
  let inHunk = false;

  for (const line of lines) {
    const hunkMatch = line.match(HUNK_HEADER_RE);
    if (hunkMatch) {
      flushBlock(deletions, additions, rows);
      oldLn = parseInt(hunkMatch[1], 10);
      newLn = parseInt(hunkMatch[2], 10);
      inHunk = true;
      rows.push({ type: 'hunk', left: null, right: null, hunkText: line });
      continue;
    }

    if (!inHunk) continue; // skip file headers before the first hunk

    const marker = line[0];
    const content = line.slice(1);

    if (marker === '+') {
      additions.push({ lineNumber: newLn++, content });
    } else if (marker === '-') {
      deletions.push({ lineNumber: oldLn++, content });
    } else if (marker === '\\') {
      // "\ No newline at end of file" — not a real line, ignore
      continue;
    } else {
      // context line: appears identically on both sides
      flushBlock(deletions, additions, rows);
      rows.push({
        type: 'context',
        left: { lineNumber: oldLn++, content },
        right: { lineNumber: newLn++, content },
      });
    }
  }

  flushBlock(deletions, additions, rows);
  return rows;
}

/** Background tint for a half-row based on the row type and which side it is */
function cellBg(type: RowType, side: 'left' | 'right'): string {
  switch (type) {
    case 'deletion':
      return side === 'left' ? 'bg-destructive/10' : 'bg-muted/30';
    case 'addition':
      return side === 'right' ? 'bg-success/10' : 'bg-muted/30';
    case 'modified':
      return side === 'left' ? 'bg-destructive/10' : 'bg-success/10';
    default:
      return '';
  }
}

function cellText(type: RowType, side: 'left' | 'right'): string {
  if (type === 'modified') {
    return side === 'left' ? 'text-destructive' : 'text-success';
  }
  if (type === 'deletion' && side === 'left') return 'text-destructive';
  if (type === 'addition' && side === 'right') return 'text-success';
  return 'text-foreground/80';
}

function DiffHalf({
  cell,
  type,
  side,
}: {
  cell: DiffCell | null;
  type: RowType;
  side: 'left' | 'right';
}) {
  const isFiller = !cell;
  return (
    <>
      <td
        className={cn(
          'select-none text-right align-top pr-2 pl-2 w-12 text-muted-foreground/50 tabular-nums',
          cellBg(type, side)
        )}
      >
        {cell?.lineNumber ?? ''}
      </td>
      <td
        className={cn(
          'align-top pl-2 pr-3 w-1/2',
          cellBg(type, side),
          isFiller && 'bg-muted/20'
        )}
      >
        <pre
          className={cn(
            'whitespace-pre-wrap break-words m-0 font-mono',
            cellText(type, side)
          )}
        >
          {cell ? cell.content || ' ' : ' '}
        </pre>
      </td>
    </>
  );
}

/**
 * Side-by-side ("split") diff viewer in the style of an IDE diff window:
 * the old version of the file on the left, the new version on the right,
 * with changed/added/removed lines highlighted and aligned row-by-row.
 *
 * @example
 * ```tsx
 * <SplitDiffViewer diff={file.diff} maxHeight="400px" />
 * ```
 */
export function SplitDiffViewer({
  diff,
  maxHeight = '400px',
  className,
}: SplitDiffViewerProps) {
  const rows = useMemo(() => parseSplitDiff(diff), [diff]);

  if (!diff || rows.length === 0) {
    return (
      <div className={cn('text-center py-4 text-muted-foreground text-sm', className)}>
        No diff content available
      </div>
    );
  }

  return (
    <ScrollArea className={cn('w-full rounded-md bg-muted/50', className)} style={{ maxHeight }}>
      <table className="w-full table-fixed border-collapse text-xs leading-relaxed">
        <colgroup>
          <col className="w-12" />
          <col />
          <col className="w-12" />
          <col />
        </colgroup>
        <tbody>
          {rows.map((row, idx) => {
            if (row.type === 'hunk') {
              return (
                <tr key={idx}>
                  <td
                    colSpan={4}
                    className="bg-info/10 text-info font-mono px-3 py-0.5 select-none"
                  >
                    {row.hunkText}
                  </td>
                </tr>
              );
            }
            return (
              <tr key={idx} className="border-b border-border/30 last:border-b-0">
                <DiffHalf cell={row.left} type={row.type} side="left" />
                <DiffHalf cell={row.right} type={row.type} side="right" />
              </tr>
            );
          })}
        </tbody>
      </table>
    </ScrollArea>
  );
}

export default SplitDiffViewer;
