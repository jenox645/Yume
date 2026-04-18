// ============================================================================
// DEBUG SYSTEM - Comprehensive Logging and Error Tracking
// ============================================================================

class DebugSystem {
  constructor() {
    this.logs = [];
    this.errors = [];
    this.maxLogs = 500;
    this.startTime = Date.now();
    this.sessionId = this.generateSessionId();
    
    // Console styling
    this.styles = {
      ERROR: 'background: #ef4444; color: white; padding: 2px 6px; border-radius: 3px; font-weight: bold',
      WARN: 'background: #f59e0b; color: white; padding: 2px 6px; border-radius: 3px; font-weight: bold',
      INFO: 'background: #3b82f6; color: white; padding: 2px 6px; border-radius: 3px; font-weight: bold',
      SUCCESS: 'background: #10b981; color: white; padding: 2px 6px; border-radius: 3px; font-weight: bold',
      DEBUG: 'background: #6b7280; color: white; padding: 2px 6px; border-radius: 3px',
      STATE: 'background: #8b5cf6; color: white; padding: 2px 6px; border-radius: 3px; font-weight: bold'
    };
    
    this.init();
  }
  
  generateSessionId() {
    // Non-security: identifies debug log groups in console only, never used as a secret or token
    return `session-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  }
  
  init() {
    console.log('%c DEBUG SYSTEM INITIALIZED', 'font-size: 16px; font-weight: bold; color: #3b82f6');
    console.log(`%cSession ID: ${this.sessionId}`, 'color: #6b7280');
    console.log(`%cTimestamp: ${new Date().toISOString()}`, 'color: #6b7280');
    console.log('%c' + '='.repeat(80), 'color: #d1d5db');
    
    // Capture uncaught errors
    window.addEventListener('error', (e) => {
      this.error('UncaughtError', e.message, {
        filename: e.filename,
        lineno: e.lineno,
        colno: e.colno
      });
    });
    
    window.addEventListener('unhandledrejection', (e) => {
      this.error('UnhandledPromise', e.reason, {
        promise: e.promise
      });
    });
  }
  
  // Main logging method
  log(level, component, message, data = {}) {
    const entry = {
      timestamp: Date.now(),
      elapsed: Date.now() - this.startTime,
      level,
      component,
      message,
      data: this.sanitizeData(data),
      sessionId: this.sessionId
    };
    
    this.logs.push(entry);
    if (this.logs.length > this.maxLogs) {
      this.logs.shift();
    }
    
    if (level === 'ERROR') {
      this.errors.push(entry);
    }
    
    this.outputToConsole(entry);
  }
  
  sanitizeData(data) {
    try {
      return JSON.parse(JSON.stringify(data));
    } catch (_e) {
      return { _error: 'Could not serialize data' };
    }
  }
  
  outputToConsole(entry) {
    const time = new Date(entry.timestamp).toLocaleTimeString('en-US', { 
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      fractionalSecondDigits: 3
    });
    
    const elapsed = `+${(entry.elapsed / 1000).toFixed(2)}s`;
    const style = this.styles[entry.level] || this.styles.DEBUG;
    
    const prefix = `%c${entry.level}%c [${time}] [${elapsed}] [${entry.component}]`;
    const styles = [style, 'color: inherit'];
    
    if (Object.keys(entry.data).length > 0) {
      console.log(prefix, ...styles, entry.message, entry.data);
    } else {
      console.log(prefix, ...styles, entry.message);
    }
  }
  
  // Convenience methods
  error(component, message, data = {}) {
    this.log('ERROR', component, message, data);
    // Also log stack trace
    console.trace('Error stack trace:');
  }
  
  warn(component, message, data = {}) {
    this.log('WARN', component, message, data);
  }
  
  info(component, message, data = {}) {
    this.log('INFO', component, message, data);
  }
  
  success(component, message, data = {}) {
    this.log('SUCCESS', component, message, data);
  }
  
  debug(component, message, data = {}) {
    this.log('DEBUG', component, message, data);
  }
  
  state(component, stateName, stateValue) {
    this.log('STATE', component, `State: ${stateName}`, { value: stateValue });
  }
  
  // Function call tracking
  functionStart(component, functionName, args = {}) {
    this.debug(component, ` ${functionName}()`, { args });
  }
  
  functionEnd(component, functionName, result = {}) {
    this.debug(component, ` ${functionName}()`, { result });
  }
  
  // Export logs
  exportLogs() {
    const exportData = {
      sessionId: this.sessionId,
      exportTime: new Date().toISOString(),
      totalLogs: this.logs.length,
      totalErrors: this.errors.length,
      logs: this.logs,
      errors: this.errors,
      browser: navigator.userAgent,
      url: window.location.href
    };
    
    return JSON.stringify(exportData, null, 2);
  }
  
  downloadLogs() {
    const data = this.exportLogs();
    const blob = new Blob([data], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `subtitle-debug-${this.sessionId}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    
    this.success('DebugSystem', 'Logs downloaded', { 
      filename: a.download,
      totalLogs: this.logs.length 
    });
  }
  
  // Get summary
  getSummary() {
    const errorCount = this.errors.length;
    const warnCount = this.logs.filter(l => l.level === 'WARN').length;
    const infoCount = this.logs.filter(l => l.level === 'INFO').length;
    
    return {
      sessionId: this.sessionId,
      totalLogs: this.logs.length,
      errors: errorCount,
      warnings: warnCount,
      info: infoCount,
      runtime: `${((Date.now() - this.startTime) / 1000).toFixed(2)}s`,
      lastError: this.errors[this.errors.length - 1] || null
    };
  }
  
  printSummary() {
    const summary = this.getSummary();
    console.log('%c DEBUG SUMMARY', 'font-size: 14px; font-weight: bold; color: #3b82f6');
    console.table(summary);
  }
  
  // Clear logs
  clear() {
    this.logs = [];
    this.errors = [];
    console.clear();
    this.info('DebugSystem', 'Logs cleared');
  }
}

// Global instance
// eslint-disable-next-line no-redeclare
const DEBUG = new DebugSystem();

// Make available globally
if (typeof window !== 'undefined') {
  window.DEBUG = DEBUG;
  
  // Add console commands
  window.debugDownload = () => DEBUG.downloadLogs();
  window.debugSummary = () => DEBUG.printSummary();
  window.debugClear = () => DEBUG.clear();
  
  console.log('%cDebug Commands Available:', 'font-weight: bold; color: #3b82f6');
  console.log('%c  debugDownload()  %c- Download all logs as JSON', 'color: #10b981', 'color: inherit');
  console.log('%c  debugSummary()   %c- Show log summary', 'color: #10b981', 'color: inherit');
  console.log('%c  debugClear()     %c- Clear all logs', 'color: #10b981', 'color: inherit');
  console.log('%c' + '='.repeat(80), 'color: #d1d5db');
}
