import { X, FileText, AlertCircle } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { ChatAttachment } from '../../shared/types';
import { MAX_IMAGE_SIZE, ALLOWED_IMAGE_TYPES } from '../../shared/constants';
import {
  fileToBase64,
  createThumbnail,
  isValidImageType,
  resolveFilename,
  generateImageId,
  formatFileSize,
} from '../ImageUpload';

/** Per-file cap for inlined text/code attachments (their contents go into the prompt). */
export const MAX_TEXT_ATTACHMENT_SIZE = 256 * 1024; // 256 KB

/** Per-file cap for binary document attachments (PDF/DOCX). */
export const MAX_DOCUMENT_ATTACHMENT_SIZE = 10 * 1024 * 1024; // 10 MB

/** Max attachments per message (images + text + documents combined). */
export const MAX_CHAT_ATTACHMENTS = 10;

// File extensions we treat as inlinable text/code even when the browser reports
// an empty or generic MIME type (common for source files like .ts, .py, .go).
const TEXT_EXTENSIONS = [
  '.txt', '.md', '.markdown', '.json', '.jsonc', '.yaml', '.yml', '.toml', '.ini',
  '.cfg', '.conf', '.env', '.csv', '.tsv', '.xml', '.html', '.htm', '.css', '.scss',
  '.less', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.py', '.rb', '.go', '.rs',
  '.java', '.kt', '.kts', '.c', '.h', '.cpp', '.hpp', '.cc', '.cs', '.php', '.swift',
  '.sh', '.bash', '.zsh', '.fish', '.ps1', '.sql', '.graphql', '.gql', '.proto',
  '.vue', '.svelte', '.astro', '.dockerfile', '.gitignore', '.lock', '.log',
];

const TEXT_MIME_TYPES = [
  'application/json', 'application/xml', 'application/javascript',
  'application/typescript', 'application/x-yaml', 'application/x-sh',
  'application/x-httpd-php',
];

// Rich binary documents (PDF / Word). Sent as raw bytes (base64) and read by
// the agent from disk — never inlined as UTF-8 text in the browser.
const DOCUMENT_EXTENSIONS = ['.pdf', '.docx'];

const DOCUMENT_MIME_TYPES = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
];

/** Accept attribute for the chat file picker: images, PDF/DOCX docs, and text/code. */
export const CHAT_FILE_ACCEPT = [
  ...ALLOWED_IMAGE_TYPES,
  ...DOCUMENT_MIME_TYPES,
  ...DOCUMENT_EXTENSIONS,
  'text/*',
  ...TEXT_EXTENSIONS,
].join(',');

/** Whether a file is a supported rich document (PDF or Word), sent as raw bytes. */
function looksLikeDocument(file: File): boolean {
  if (DOCUMENT_MIME_TYPES.includes(file.type)) return true;
  const lower = file.name.toLowerCase();
  return DOCUMENT_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/** Whether a file should be treated as an inlinable text/code attachment. */
function looksLikeText(file: File): boolean {
  if (file.type.startsWith('text/')) return true;
  if (TEXT_MIME_TYPES.includes(file.type)) return true;
  const lower = file.name.toLowerCase();
  return TEXT_EXTENSIONS.some((ext) => lower.endsWith(ext)) || !lower.includes('.');
}

/** Base64-encode a UTF-8 string in a browser-safe way (handles non-Latin1 chars). */
function utf8ToBase64(str: string): string {
  return btoa(unescape(encodeURIComponent(str)));
}

/**
 * Turn picked/pasted/dropped files into ChatAttachments. Images are base64'd
 * with a thumbnail; text/code files have their UTF-8 contents base64'd for a
 * uniform transport contract. Returns the new attachments plus any user-facing
 * errors (size/type/count). Shared by the file picker, paste, and drop paths.
 */
export async function processChatFiles(
  files: FileList | File[],
  existing: ChatAttachment[],
): Promise<{ attachments: ChatAttachment[]; errors: string[] }> {
  const errors: string[] = [];
  const out: ChatAttachment[] = [];

  const slots = MAX_CHAT_ATTACHMENTS - existing.length;
  if (slots <= 0) {
    errors.push(`Maximum of ${MAX_CHAT_ATTACHMENTS} attachments allowed`);
    return { attachments: out, errors };
  }

  const all = Array.from(files);
  const toProcess = all.slice(0, slots);
  if (all.length > slots) {
    errors.push(`Only ${slots} more attachment(s) can be added. Some files were skipped.`);
  }

  const takenNames = existing.map((a) => a.filename);

  for (const file of toProcess) {
    const isImage = isValidImageType(file);
    const isDoc = !isImage && looksLikeDocument(file);
    const isText = !isImage && !isDoc && looksLikeText(file);

    if (!isImage && !isDoc && !isText) {
      errors.push(`"${file.name}" is not a supported type (images, PDF/DOCX documents, or text/code files only).`);
      continue;
    }

    try {
      const filename = resolveFilename(file.name, [...takenNames, ...out.map((a) => a.filename)]);

      if (isDoc) {
        if (file.size > MAX_DOCUMENT_ATTACHMENT_SIZE) {
          errors.push(`"${file.name}" exceeds ${formatFileSize(MAX_DOCUMENT_ATTACHMENT_SIZE)}. Attach a smaller document.`);
          continue;
        }
        const lower = filename.toLowerCase();
        const dataUrl = await fileToBase64(file); // raw bytes, base64 (data-URL prefix stripped below)
        out.push({
          id: generateImageId(),
          kind: 'document',
          filename,
          mimeType:
            file.type ||
            (lower.endsWith('.pdf')
              ? 'application/pdf'
              : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
          size: file.size,
          data: dataUrl.split(',')[1],
        });
      } else if (isImage) {
        if (file.size > MAX_IMAGE_SIZE) {
          errors.push(`"${file.name}" is larger than 10MB. Consider compressing it.`);
          // Still allow the upload, just warn (mirrors task image-upload behavior).
        }
        const dataUrl = await fileToBase64(file);
        const thumbnail = await createThumbnail(dataUrl);
        out.push({
          id: generateImageId(),
          kind: 'image',
          filename,
          mimeType: file.type,
          size: file.size,
          data: dataUrl.split(',')[1], // base64 without the data-URL prefix
          thumbnail,
        });
      } else {
        if (file.size > MAX_TEXT_ATTACHMENT_SIZE) {
          errors.push(`"${file.name}" exceeds ${formatFileSize(MAX_TEXT_ATTACHMENT_SIZE)}. Attach a smaller file.`);
          continue;
        }
        const text = await file.text();
        out.push({
          id: generateImageId(),
          kind: 'text',
          filename,
          mimeType: file.type || 'text/plain',
          size: file.size,
          data: utf8ToBase64(text),
        });
      }
    } catch {
      errors.push(`Failed to process "${file.name}"`);
    }
  }

  return { attachments: out, errors };
}

interface ChatAttachmentBarProps {
  attachments: ChatAttachment[];
  /** Remove a single attachment by id. Omit to render read-only (e.g. in history). */
  onRemove?: (id: string) => void;
  error?: string | null;
  className?: string;
}

/**
 * Presentational chip row for chat attachments. Images render as thumbnails,
 * text/code files as labeled chips. Used both above the input (with onRemove)
 * and inside the user message bubble (read-only).
 */
export function ChatAttachmentBar({ attachments, onRemove, error, className }: ChatAttachmentBarProps) {
  if (attachments.length === 0 && !error) return null;

  return (
    <div className={cn('space-y-2', className)}>
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {attachments.map((att) => (
            <div
              key={att.id}
              className="group relative flex items-center gap-2 rounded-md border border-border bg-muted/40 py-1 pl-1 pr-2"
            >
              {att.kind === 'image' && att.thumbnail ? (
                <img
                  src={att.thumbnail}
                  alt={att.filename}
                  className="h-8 w-8 shrink-0 rounded object-cover"
                />
              ) : (
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-muted">
                  <FileText className="h-4 w-4 text-muted-foreground" />
                </div>
              )}
              <div className="min-w-0">
                <p className="max-w-[160px] truncate text-xs font-medium text-foreground">
                  {att.filename}
                </p>
                <p className="text-[10px] text-muted-foreground">
                  {att.kind === 'image' ? 'Image' : att.kind === 'document' ? 'Document' : 'Text'} · {formatFileSize(att.size)}
                </p>
              </div>
              {onRemove && (
                <button
                  type="button"
                  onClick={() => onRemove(att.id)}
                  className="ml-1 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-muted-foreground hover:bg-destructive hover:text-destructive-foreground"
                  aria-label={`Remove ${att.filename}`}
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
    </div>
  );
}
