// GRACE-2 web - public landing page (v2).
//
// The hero/marketing page rendered at "/" for first-time visitors (and at
// "/landing" always - see EntryRouter.tsx for the passthrough rule). Pure
// presentational React + CSS + one hand-rolled canvas backdrop: no router, no
// UI kit, no heavy assets, no new dependencies.
//
// CTA + routing contract (PRESERVE - EntryRouter and live-verify depend on it):
//   - the primary CTA points at "/app" with data-testid "grace2-landing-cta";
//   - the "Resume session" variant renders when hasSession is true (returning
//     visitors reaching "/landing");
//   - the privacy link points at "/privacy" (data-testid
//     "grace2-landing-privacy-link").
//
// Design language (v2 - deliberately distinct from v1):
//   - a deep, near-black scientific palette with ONE restrained cool accent
//     (no rainbow gradient, no vendor logos, no count-bragging);
//   - lead motif is an animated, cursor-interactive topographic CONTOUR FIELD
//     drawn on a <canvas> (ContourField.tsx) behind the hero - it bends toward
//     the pointer and slowly drifts, evoking terrain / flood surfaces;
//   - generous whitespace, a clear type hierarchy, and scroll-reveal
//     transitions (IntersectionObserver) that degrade to instant-visible under
//     prefers-reduced-motion (handled in landing.css and the canvas itself).
//
// Copy leads with OUTCOME, not architecture: what defensible, fast multi-hazard
// analysis enables - grounded in real numerical simulation, not estimates.

import { useEffect, useRef } from "react";
import type { FC } from "react";
import {
  IconChat,
  IconWaves,
  IconGrid,
  IconTerrain,
  IconGlobe,
  IconFlowArrow,
  IconModel,
  IconMapPin,
  IconArrowRight,
} from "../components/icons";
import type { IconProps } from "../components/icons";
import { ContourField } from "../components/landing/ContourField";
import "./landing.css";

export interface LandingProps {
  /**
   * True when the browser already carries a GRACE-2 session key - only
   * reachable via the explicit "/landing" path in that case (EntryRouter
   * passes "/" straight through to the app). Switches the primary CTA to the
   * "Resume session" variant.
   */
  hasSession?: boolean;
}

interface Capability {
  icon: FC<IconProps>;
  title: string;
  body: string;
}

/**
 * Capabilities framed as plain-English OUTCOMES for a general/professional
 * audience - no engine name-drops, no jargon. Every one is backed by a real
 * numerical solver under the hood; the page sells the result, not the tool.
 */
const CAPABILITIES: Capability[] = [
  {
    icon: IconWaves,
    title: "Coastal flooding and storm surge",
    body:
      "See how far the water reaches when a storm pushes the ocean inland - surge and wave run-up resolved across the coastline and the streets behind it.",
  },
  {
    icon: IconFlowArrow,
    title: "River and compound flooding",
    body:
      "Combine rainfall, river discharge, and surge in a single run to understand the floods that build when several drivers arrive at once.",
  },
  {
    icon: IconGrid,
    title: "Urban drainage and pluvial flooding",
    body:
      "Trace where rain pools in a city block by block, with buildings, walls, and drainage structures shaping the flow the way they do on the ground.",
  },
  {
    icon: IconGlobe,
    title: "Groundwater and contamination",
    body:
      "Follow a contaminant plume through an aquifer from source to receptor, so you know what is at risk and when it arrives.",
  },
  {
    icon: IconTerrain,
    title: "Earthquake and terrain hazard",
    body:
      "Map expected ground shaking and the slopes most primed to fail, turning regional hazard into something you can act on locally.",
  },
  {
    icon: IconMapPin,
    title: "Damage and loss to what matters",
    body:
      "Score real buildings against the modeled hazard to estimate damage and loss - the human and economic stakes, not just a depth map.",
  },
];

interface Step {
  n: string;
  icon: FC<IconProps>;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  {
    n: "01",
    icon: IconChat,
    title: "Describe it",
    body:
      "State the scenario in plain language - a hundred-year flood for a stretch of coast, the damage to the buildings behind it. Draw the area on the map if the bounds matter.",
  },
  {
    n: "02",
    icon: IconModel,
    title: "It runs the science",
    body:
      "Authoritative elevation, weather, and infrastructure data are assembled into a model and solved with the same numerical engines specialists rely on - not an estimate or a guess.",
  },
  {
    n: "03",
    icon: IconMapPin,
    title: "Read the result",
    body:
      "Results paint onto an interactive map you can pan, scrub through time, and export - every number traceable to a simulation or a source you can name.",
  },
];

const IMPACTS = [
  { n: "Weeks to minutes", l: "Analysis that took a specialist team weeks, asked for and answered in a single sitting." },
  { n: "Grounded in physics", l: "Every result comes from a real numerical simulation - never an estimate dressed up as one." },
  { n: "Built to defend", l: "Each value is traceable to the data and the model behind it, so the work holds up to scrutiny." },
];

/** Lightweight scroll-reveal: adds `is-in` to observed nodes once on screen.
 *
 * IMPORTANT ordering: React calls the `ref={reveal}` callbacks during the commit
 * phase, which is BEFORE `useEffect` runs -- so at registration time the observer
 * does not exist yet. The previous version dropped those nodes on the floor
 * (`el && observer.current` was false for every section), so NOTHING was ever
 * observed, `.is-in` was never added, and every `.lp-section` stayed at
 * `opacity: 0` -- the live "nothing renders past the hero" bug (only
 * prefers-reduced-motion users escaped, via the CSS override). Fix: queue nodes
 * registered before the effect into `pending`, then observe them once the
 * observer is created; and show everything immediately when reveal is disabled. */
function useScrollReveal(): (el: HTMLElement | null) => void {
  const observer = useRef<IntersectionObserver | null>(null);
  const pending = useRef<Set<HTMLElement>>(new Set());

  useEffect(() => {
    const reduce =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || typeof IntersectionObserver === "undefined") {
      // No reveal animation available: reveal every queued section now so the
      // page is never stuck invisible (belt-and-suspenders with landing.css).
      for (const el of pending.current) el.classList.add("is-in");
      pending.current.clear();
      return;
    }
    const obs = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-in");
            obs.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" },
    );
    observer.current = obs;
    // Observe any nodes that registered before this effect ran (the commit-phase
    // ref callbacks) -- without this they are never observed and stay hidden.
    for (const el of pending.current) obs.observe(el);
    pending.current.clear();
    return () => {
      obs.disconnect();
      observer.current = null;
    };
  }, []);

  return (el: HTMLElement | null) => {
    if (!el) return;
    if (observer.current) observer.current.observe(el);
    else pending.current.add(el);
  };
}

export function Landing({ hasSession = false }: LandingProps): JSX.Element {
  useEffect(() => {
    document.title =
      "TRID3NT - Conversational multi-hazard analysis, grounded in real simulation";
  }, []);

  const reveal = useScrollReveal();
  const ctaLabel = hasSession ? "Resume session" : "Launch TRID3NT";

  return (
    <div className="lp" data-testid="grace2-landing">
      <header className="lp-nav">
        <a className="lp-wordmark" href="/">
          <span className="lp-wordmark-glyph" aria-hidden="true" />
          TRID3NT
        </a>
        <nav className="lp-nav-links" aria-label="Landing navigation">
          <a href="#capabilities">Capabilities</a>
          <a href="#how">How it works</a>
          <a href="#impact">Why it matters</a>
          <a href="/privacy">Privacy</a>
          <a className="lp-nav-launch" href="/app">
            Launch app
          </a>
        </nav>
      </header>

      <main>
        {/* ------------------------- Hero (split) ------------------------- */}
        {/* Desktop: two columns -- copy/CTA left, flagship screenshot right.
            The contour field sits subtly behind the whole hero (veiled down).
            At mobile widths (<=940px) the grid collapses to a single column and
            the large hero shot is hidden (display:none in landing.css), so the
            mobile stack stays the current single-column copy-only hero. */}
        <section className="lp-hero">
          <div className="lp-hero-field" aria-hidden="true">
            <ContourField className="lp-contours" />
            <div className="lp-hero-veil" />
          </div>

          <div className="lp-hero-grid">
            <div className="lp-hero-inner">
              <span className="lp-eyebrow">Multi-hazard modeling, made conversational</span>
              <h1 className="lp-h1">
                Understand the hazard
                <br />
                <span className="lp-accent">before it arrives.</span>
              </h1>
              <p className="lp-sub">
                TRID3NT turns a question about flood, storm surge, groundwater,
                earthquake, or wildfire risk into a rigorous answer - in plain
                language, on a live map, grounded in real numerical simulation.
                Work that once took a team of specialists weeks now happens in a
                single conversation.
              </p>
              <div className="lp-cta-row">
                <a
                  className="lp-cta"
                  href="/app"
                  data-testid="grace2-landing-cta"
                >
                  {ctaLabel}
                  <span className="lp-cta-arrow" aria-hidden="true">
                    <IconArrowRight size={16} />
                  </span>
                </a>
                <a className="lp-cta-ghost" href="#how">
                  See how it works
                </a>
              </div>
            </div>

            {/* Flagship product shot -- desktop only (hidden at <=940px). The
                flood render now lives here, so the old lp-showcase flood section
                below is removed to avoid duplicating this image. */}
            <figure className="lp-hero-shot" aria-hidden="true">
              <img
                src="/landing/shot_flood_desktop.webp"
                width={1440}
                height={900}
                alt=""
                loading="eager"
              />
            </figure>
          </div>

          <a className="lp-scroll-cue" href="#capabilities" aria-label="Scroll to capabilities">
            <span className="lp-scroll-dot" aria-hidden="true" />
          </a>
        </section>

        {/* ----------------------- Capabilities ----------------------- */}
        <section
          className="lp-section lp-cap"
          id="capabilities"
          ref={reveal}
        >
          <div className="lp-section-head">
            <span className="lp-kicker">What you can ask</span>
            <h2 className="lp-h2">
              One conversation, the hazards that shape a place.
            </h2>
            <p className="lp-section-sub">
              Describe the risk you care about in everyday terms. Behind each
              answer is a full physics-based simulation - the same class of
              model a specialist would build by hand, run for you and rendered
              where you can interrogate it.
            </p>
          </div>
          <div className="lp-cap-grid">
            {CAPABILITIES.map((c) => (
              <article
                key={c.title}
                className="lp-card"
                data-testid="grace2-landing-capability"
              >
                <span className="lp-card-icon" aria-hidden="true">
                  <c.icon size={24} />
                </span>
                <h3>{c.title}</h3>
                <p>{c.body}</p>
              </article>
            ))}
          </div>
        </section>

        {/* NOTE: the flood-render showcase that used to live here was removed --
            the flagship flood screenshot now anchors the split hero above, so
            rendering it again here would duplicate the image. The remaining
            showcase below features the OTHER shot (shot_impact_desktop.webp). */}

        {/* ----------------------- How it works ----------------------- */}
        <section className="lp-section lp-how" id="how" ref={reveal}>
          <div className="lp-section-head">
            <span className="lp-kicker">How it works</span>
            <h2 className="lp-h2">From a sentence to a defensible result.</h2>
            <p className="lp-section-sub">
              No desktop GIS, no model decks, no file wrangling. Three steps
              take you from a question to an answer you can stand behind.
            </p>
          </div>
          <ol className="lp-steps">
            {STEPS.map((s, i) => (
              <li className="lp-step" key={s.n}>
                <span className="lp-step-n" aria-hidden="true">
                  {s.n}
                </span>
                <span className="lp-step-icon" aria-hidden="true">
                  <s.icon size={22} />
                </span>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
                {i < STEPS.length - 1 && (
                  <span className="lp-step-arrow" aria-hidden="true">
                    <IconArrowRight size={18} />
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>

        {/* ----------------------- Impact band ----------------------- */}
        <section className="lp-section lp-impact" id="impact" ref={reveal}>
          <div className="lp-impact-grid">
            <div className="lp-impact-copy">
              <span className="lp-kicker">Why it matters</span>
              <h2 className="lp-h2">
                Faster answers, held to a higher standard.
              </h2>
              <p>
                The hard part of hazard work was never having an opinion - it
                was producing one you could defend. TRID3NT keeps the rigor and
                removes the friction: a conversational workbench in front of the
                real simulation engines, so the analysis is fast to reach and
                sound enough to act on.
              </p>
              <p className="lp-impact-line">
                The model plans, explains, and assembles the work - but it never
                invents a number. Every value on the map comes from a numerical
                simulation or an authoritative source, and carries the
                provenance to prove it.
              </p>
            </div>
            <ul className="lp-impact-list">
              {IMPACTS.map((m) => (
                <li className="lp-impact-item" key={m.n}>
                  <span className="lp-impact-n">{m.n}</span>
                  <span className="lp-impact-l">{m.l}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="lp-impact-shots">
            {/* IMAGE SLOTS: real North-Star mobile screenshots.
                Orchestrator swaps live evidence into these stable filenames. */}
            <figure className="lp-phone">
              <img
                src="/landing/shot_chat_mobile.webp"
                width={390}
                height={844}
                alt="TRID3NT on a phone, working through a hazard analysis as a conversation"
                loading="lazy"
              />
              <figcaption>The analysis, narrated as it runs.</figcaption>
            </figure>
            <figure className="lp-phone lp-phone-offset">
              <img
                src="/landing/shot_terrain_mobile.webp"
                width={390}
                height={844}
                alt="A colored-relief terrain layer rendered on the TRID3NT mobile map"
                loading="lazy"
              />
              <figcaption>The terrain it produced, on the map.</figcaption>
            </figure>
          </div>
        </section>

        {/* --------------------- Impact showcase --------------------- */}
        <section className="lp-section lp-showcase lp-showcase-wide" ref={reveal}>
          <figure className="lp-frame">
            {/* IMAGE SLOT: real North-Star desktop screenshot (impact/damage view).
                Orchestrator swaps live evidence into /landing/shot_impact_desktop.webp.
                Minimalist: no fake browser-dots bar -- a clean framed render. */}
            <img
              src="/landing/shot_impact_desktop.webp"
              width={1440}
              height={900}
              alt="TRID3NT estimating damage to individual buildings beneath a simulated flood-depth surface"
              loading="lazy"
            />
            <figcaption>
              Damage estimated building by building - the stakes beneath the
              hazard, not just the depth.
            </figcaption>
          </figure>
        </section>

        {/* ----------------------- Bottom CTA ----------------------- */}
        <section className="lp-section lp-bottom" ref={reveal}>
          <h2 className="lp-h2">
            Ask the next hard question <span className="lp-accent">out loud.</span>
          </h2>
          <p className="lp-section-sub lp-bottom-sub">
            Open the workbench and put a real hazard scenario to it.
          </p>
          <a className="lp-cta" href="/app">
            {ctaLabel}
            <span className="lp-cta-arrow" aria-hidden="true">
              <IconArrowRight size={16} />
            </span>
          </a>
        </section>
      </main>

      <footer className="lp-footer">
        <div className="lp-footer-row">
          <span className="lp-footer-brand">
            <span className="lp-wordmark-glyph" aria-hidden="true" />
            TRID3NT
          </span>
          <nav aria-label="Footer">
            <a href="/privacy" data-testid="grace2-landing-privacy-link">
              Privacy Policy
            </a>
            <a href="mailto:natealmanza3@gmail.com">Contact</a>
          </nav>
        </div>
        <p className="lp-footer-fine">
          - 2026 TRID3NT. Model outputs are research aids, not official
          hazard guidance.
        </p>
      </footer>
    </div>
  );
}
