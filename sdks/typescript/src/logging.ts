/** Minimal logging seam so hosts can capture SDK diagnostics. */
export interface ControlLogger {
  debug(message: string, ...args: unknown[]): void;
  info(message: string, ...args: unknown[]): void;
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
}

const PREFIX = "[agent-control]";

export const consoleLogger: ControlLogger = {
  debug: () => undefined,
  info: (message, ...args) => console.info(`${PREFIX} ${message}`, ...args),
  warn: (message, ...args) => console.warn(`${PREFIX} ${message}`, ...args),
  error: (message, ...args) => console.error(`${PREFIX} ${message}`, ...args),
};

export const silentLogger: ControlLogger = {
  debug: () => undefined,
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined,
};
