#!/usr/bin/env node

// SPDX-License-Identifier: MIT
// Copyright (c) 2026 coccinella-labs

/**
 * manual.js
 * A CLI script for manual code fixing using Gemini AI.
 * Analyzes local files and provides suggestions for improvements.
 * Usage: node manual.js [files...]
 */

const fs = require('fs');
const path = require('path');
const { GoogleGenerativeAI } = require('@google/generative-ai');

function getFilesToAnalyze(args) {
  if (args.length > 0) {
    return args;
  }
  // Default to common source files
  return ['**/*.js', '**/*.ts', '**/*.py', '**/*.rb', '**/*.go', '**/*.rs', '**/*.java'];
}

function readFileContent(file) {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (e) {
    console.error(`Error reading ${file}: ${e.message}`);
    return null;
  }
}

function getFilesWithExt(dir, ext) {
  const files = [];
  try {
    const items = fs.readdirSync(dir, { withFileTypes: true });
    for (const item of items) {
      const fullPath = path.join(dir, item.name);
      if (item.isDirectory() && !item.name.startsWith('.')) { // Skip hidden dirs
        files.push(...getFilesWithExt(fullPath, ext));
      } else if (item.isFile() && item.name.endsWith('.' + ext)) {
        files.push(fullPath);
      }
    }
  } catch (e) {
    // Ignore errors for inaccessible dirs
  }
  return files;
}

function expandGlobs(patterns) {
  const files = [];
  for (const pattern of patterns) {
    if (pattern.startsWith('**/*.')) {
      const ext = pattern.split('.').pop();
      files.push(...getFilesWithExt('.', ext));
    } else {
      files.push(pattern);
    }
  }
  return files;
}

async function analyzeWithGemini(files, apiKey) {
  const genAI = new GoogleGenerativeAI(apiKey);
  const model = genAI.getGenerativeModel({ model: 'gemini-1.5-flash' });

  let content = 'Analyze the following code files for improvements. Focus on code quality, best practices, performance, and correctness.\n\n';

  for (const file of files) {
    const fileContent = readFileContent(file);
    if (fileContent) {
      content += `File: ${file}\n\`\`\`\n${fileContent}\n\`\`\`\n\n`;
    }
  }

  content += 'Provide specific code suggestions as diff blocks in the format:\n```diff\nfile_path\n@@ -line_start,count +line_start,count @@\n- old code\n+ new code\n```\n\nSuggestions should be actionable.';

  const result = await model.generateContent(content);
  return result.response.text();
}

function parseSuggestions(analysis) {
  const suggestions = [];
  const diffBlocks = analysis.match(/```diff\n([\s\S]*?)\n```/g) || [];
  for (const block of diffBlocks) {
    const content = block.replace(/```diff\n/, '').replace(/\n```/, '');
    console.log(`Suggestion:\n${content}\n`);
    suggestions.push(content);
  }
  return suggestions;
}

async function main() {
  const args = process.argv.slice(2);
  const files = expandGlobs(getFilesToAnalyze(args));

  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    console.error('Please set GEMINI_API_KEY environment variable.');
    process.exit(1);
  }

  console.log('Analyzing files:', files.join(', '));

  try {
    const analysis = await analyzeWithGemini(files, apiKey);
    console.log('\nAI Analysis:\n', analysis);

    const suggestions = parseSuggestions(analysis);
    console.log(`\nFound ${suggestions.length} suggestions.`);
  } catch (e) {
    console.error('Error:', e.message);
  }
}

main();
