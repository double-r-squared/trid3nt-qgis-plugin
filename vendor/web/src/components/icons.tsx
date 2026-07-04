/**
 * icons.tsx — SINGLE SOURCE OF TRUTH for all icon glyphs in the GRACE-2 web app.
 *
 * PROJECT UI POLICY (NATE directive): NO emojis or raw unicode glyphs are ever
 * rendered in the UI. This module is the ONLY place the app references icon
 * glyphs. Every component MUST import its icons from here — no component may
 * hardcode unicode glyphs (✕, ⟳, ▸, etc.), emoji, or import directly from
 * "@phosphor-icons/react".
 *
 * These are thin named wrapper components around the Phosphor icon pack with a
 * consistent, deliberately small API:
 *   - size?:   number   (default 16)
 *   - weight?: "regular" | "bold" | "fill"   (default "regular")
 *   - color?:  string   (default "currentColor" — inherits text color)
 *   - title?:  string   (optional accessible label; when set, the icon is
 *                        exposed to assistive tech instead of being hidden)
 *
 * Icons are decorative by default (aria-hidden=true). Pass `title` only when the
 * icon conveys meaning that is not otherwise present in adjacent text.
 *
 * Named imports from "@phosphor-icons/react" keep the bundle tree-shakeable.
 */
import type { FC } from 'react';
import {
  X,
  PencilSimple,
  Archive,
  Trash,
  DotsThreeVertical,
  DotsSixVertical,
  List,
  Gear,
  Key,
  Plus,
  Check,
  CaretDown,
  CaretRight,
  CaretLeft,
  CaretUp,
  ArrowLeft,
  ArrowRight,
  ArrowClockwise,
  Warning,
  Info,
  Globe,
  Eye,
  EyeSlash,
  Selection,
  Play,
  Pause,
  Sparkle,
  Paperclip,
  Microphone,
  PaperPlaneRight,
  ChatCircle,
  Waves,
  GridFour,
  Mountains,
  DiamondsFour,
  Hexagon,
  Brain,
  FadersHorizontal,
  Polygon,
  LineSegment,
  Scissors,
  FlowArrow,
  MapPin,
  DownloadSimple,
} from '@phosphor-icons/react';

/** Shared public API for every wrapped icon in this module. */
export interface IconProps {
  /** Square edge length in pixels. Default 16. */
  size?: number;
  /** Phosphor stroke/fill weight. Default "regular". */
  weight?: 'regular' | 'bold' | 'fill';
  /** Glyph color. Default "currentColor" so it inherits surrounding text color. */
  color?: string;
  /**
   * Optional accessible label. When provided the icon is announced to assistive
   * tech (and aria-hidden is NOT applied). When omitted the icon is decorative
   * and hidden from the a11y tree.
   */
  title?: string;
}

/**
 * Internal factory: produces a thin, typed wrapper around a Phosphor icon
 * component that enforces our consistent API and a11y defaults.
 */
type PhosphorIcon = typeof X;

function makeIcon(PhosphorComponent: PhosphorIcon, displayName: string): FC<IconProps> {
  const Wrapped: FC<IconProps> = ({
    size = 16,
    weight = 'regular',
    color = 'currentColor',
    title,
  }) => (
    <PhosphorComponent
      size={size}
      weight={weight}
      color={color}
      // Decorative by default; only expose to a11y tree when a title is given.
      aria-hidden={title ? undefined : true}
      {...(title ? { 'aria-label': title } : {})}
    />
  );
  Wrapped.displayName = displayName;
  return Wrapped;
}

export const IconClose = makeIcon(X, 'IconClose');
export const IconRename = makeIcon(PencilSimple, 'IconRename');
export const IconArchive = makeIcon(Archive, 'IconArchive');
export const IconDelete = makeIcon(Trash, 'IconDelete');
export const IconKebab = makeIcon(DotsThreeVertical, 'IconKebab');
export const IconDragHandle = makeIcon(DotsSixVertical, 'IconDragHandle');
export const IconMenu = makeIcon(List, 'IconMenu');
export const IconSettings = makeIcon(Gear, 'IconSettings');
export const IconKey = makeIcon(Key, 'IconKey');
export const IconAdd = makeIcon(Plus, 'IconAdd');
export const IconCheck = makeIcon(Check, 'IconCheck');
export const IconChevronDown = makeIcon(CaretDown, 'IconChevronDown');
export const IconChevronRight = makeIcon(CaretRight, 'IconChevronRight');
export const IconChevronLeft = makeIcon(CaretLeft, 'IconChevronLeft');
export const IconChevronUp = makeIcon(CaretUp, 'IconChevronUp');
export const IconArrowLeft = makeIcon(ArrowLeft, 'IconArrowLeft');
export const IconArrowRight = makeIcon(ArrowRight, 'IconArrowRight');
export const IconRefresh = makeIcon(ArrowClockwise, 'IconRefresh');
export const IconWarning = makeIcon(Warning, 'IconWarning');
export const IconInfo = makeIcon(Info, 'IconInfo');
export const IconGlobe = makeIcon(Globe, 'IconGlobe');
export const IconEye = makeIcon(Eye, 'IconEye');
export const IconEyeOff = makeIcon(EyeSlash, 'IconEyeOff');
export const IconBbox = makeIcon(Selection, 'IconBbox');
export const IconPlay = makeIcon(Play, 'IconPlay');
export const IconPause = makeIcon(Pause, 'IconPause');
export const IconSparkle = makeIcon(Sparkle, 'IconSparkle');
export const IconPaperclip = makeIcon(Paperclip, 'IconPaperclip');
export const IconMic = makeIcon(Microphone, 'IconMic');
export const IconSend = makeIcon(PaperPlaneRight, 'IconSend');
// Landing-page feature glyphs + sandbox/decorative icons.
export const IconChat = makeIcon(ChatCircle, 'IconChat');
export const IconWaves = makeIcon(Waves, 'IconWaves');
export const IconGrid = makeIcon(GridFour, 'IconGrid');
export const IconTerrain = makeIcon(Mountains, 'IconTerrain');
export const IconWorkspaces = makeIcon(DiamondsFour, 'IconWorkspaces');
export const IconSandbox = makeIcon(Hexagon, 'IconSandbox');
export const IconModel = makeIcon(Brain, 'IconModel');
// Composer "mode" toggle (research / standard mode). Faders glyph reads as a
// settings/mode control without the literal "Mode" text label (NATE 2026-06-17).
export const IconMode = makeIcon(FadersHorizontal, 'IconMode');
// FR-WC-16 urban vector-draw toolbar glyphs.
export const IconPolygon = makeIcon(Polygon, 'IconPolygon');
export const IconLine = makeIcon(LineSegment, 'IconLine');
export const IconSnip = makeIcon(Scissors, 'IconSnip');
export const IconFlowArrow = makeIcon(FlowArrow, 'IconFlowArrow');
export const IconMapPin = makeIcon(MapPin, 'IconMapPin');
// Station-popup "Download CSV" affordance (L3-web-station-csv). Matches the
// IconClose style - a thin wrapped Phosphor glyph, no raw unicode.
export const IconDownload = makeIcon(DownloadSimple, 'IconDownload');
