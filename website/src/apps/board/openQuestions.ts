/**
 * Parses the numbered "OPEN QUESTIONS" section rocket-plan (and similar
 * skills) post as a beads comment, so a quick-answer form can render one
 * input per question instead of asking a human to write a whole markdown
 * comment by hand - the point being a phone-friendly way to unblock a
 * story, not a general comment editor.
 */

import type { BeadsComment } from './boardApi'

export interface ParsedQuestion {
  number: number
  text: string
}

const HEADING_RE = /^#{0,3}\s*OPEN QUESTIONS\s*$/im

/** Everything after an "OPEN QUESTIONS" heading, split into numbered items.
 *  Stops at a markdown horizontal rule ("---" on its own line), which is
 *  how rocket-plan separates the questions from its trailing instruction
 *  line ("To proceed: answer these questions..."). */
export function extractOpenQuestions(text: string): ParsedQuestion[] {
  const heading = text.match(HEADING_RE)
  if (!heading || heading.index === undefined) return []
  let rest = text.slice(heading.index + heading[0].length)
  const hr = rest.match(/\n\s*-{3,}\s*\n/)
  if (hr && hr.index !== undefined) rest = rest.slice(0, hr.index)
  return rest
    .split(/\n(?=\d+\.\s)/)
    .map(s => s.trim())
    .filter(s => /^\d+\.\s/.test(s))
    .map(item => {
      const m = item.match(/^(\d+)\.\s*([\s\S]*)$/)
      return { number: m ? parseInt(m[1], 10) : 0, text: m ? m[2].trim() : item }
    })
}

/** The questions to answer right now, or null if the most recent comment
 *  isn't an OPEN QUESTIONS post - meaning either nothing is pending, or a
 *  later comment (an answer, even a freeform one) already superseded it. */
export function findPendingOpenQuestions(comments: BeadsComment[]): ParsedQuestion[] | null {
  if (!comments.length) return null;
  const last = comments[comments.length - 1];
  const questions = extractOpenQuestions(last.text);
  return questions.length ? questions : null;
}

/** Renders a set of {number: answer} entries back into the same numbered
 *  shape rocket-confirm-plan (and a human reading the thread) expects. */
export function formatAnswers(questions: ParsedQuestion[], answers: Record<number, string>): string {
  const lines = ['Answers to the open questions:', ''];
  for (const q of questions) {
    const answer = (answers[q.number] ?? '').trim();
    if (answer) lines.push(`${q.number}. ${answer}`);
  }
  return lines.join('\n');
}
