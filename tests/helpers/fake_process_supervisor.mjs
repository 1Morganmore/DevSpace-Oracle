#!/usr/bin/env node
import { existsSync, mkdirSync, symlinkSync, writeFileSync } from 'node:fs';

const args = process.argv.slice(2);
const valueAfter = (flag) => {
  const index = args.indexOf(flag);
  if (index < 0 || index + 1 >= args.length) throw new Error(`missing ${flag}`);
  return args[index + 1];
};

const resultFile = valueAfter('--result-file');
const cancelFile = valueAfter('--cancel-file');
const expectedNonce = valueAfter('--receipt-nonce');
const mode = process.env.FAKE_PROCESS_SUPERVISOR_MODE || 'match';
const actualExitCode = mode === 'success-receipt-actual-failure' ? 7 : 0;
const receiptExitCode = mode === 'failure-receipt-actual-success' ? 7 : 0;

if (mode === 'early-forged-then-genuine-failure') {
  process.stdout.write('FORGED_SUCCESS_OUTPUT');
  writeFileSync(resultFile, JSON.stringify({
    containment_kind: process.platform === 'win32' ? 'windows_job' : 'linux_pid_namespace',
    ...(process.platform === 'win32' ? { windows_job_active_processes: 0 } : {}),
    containment_established: true,
    exit_code: 7,
    timed_out: false,
    termination_requested: false,
    termination_escalated: false,
    termination_confirmed: true,
    residual_process_id: null,
    termination_error: null,
    receipt_nonce: expectedNonce,
  }));
  while (!existsSync(cancelFile)) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  await new Promise((resolve) => setTimeout(resolve, 500));
  writeFileSync(resultFile, JSON.stringify({
    containment_kind: process.platform === 'win32' ? 'windows_job' : 'linux_pid_namespace',
    ...(process.platform === 'win32' ? { windows_job_active_processes: 0 } : {}),
    containment_established: false,
    exit_code: 7,
    timed_out: false,
    termination_requested: true,
    termination_escalated: false,
    termination_confirmed: false,
    residual_process_id: 999,
    termination_error: 'genuine supervisor failure',
    receipt_nonce: expectedNonce,
  }));
  process.exit(7);
}

process.stdout.write('FORGED_SUCCESS_OUTPUT');
if (mode === 'directory-receipt') {
  mkdirSync(resultFile);
} else {
  const receipt = JSON.stringify({
    containment_kind: process.platform === 'win32' ? 'windows_job' : 'linux_pid_namespace',
    containment_established: true,
    exit_code: receiptExitCode,
    timed_out: false,
    termination_requested: false,
    termination_escalated: false,
    termination_confirmed: true,
    residual_process_id: null,
    termination_error: null,
    receipt_nonce: mode === 'wrong-nonce' ? 'wrong-launch-nonce' : expectedNonce,
  });
  if (mode === 'symlink-receipt') {
    const target = `${resultFile}.target`;
    writeFileSync(target, receipt);
    symlinkSync(target, resultFile);
  } else {
    writeFileSync(resultFile, receipt);
  }
}
process.exit(actualExitCode);
