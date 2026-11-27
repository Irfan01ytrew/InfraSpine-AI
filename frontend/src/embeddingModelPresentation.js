export function availabilityLabel(status) {
  return status.available_locally ? "Available locally" : "Unavailable locally";
}

export function loadLabel(status) {
  return status.loaded ? "Loaded in memory" : "Not loaded";
}

export function parserAvailabilityLabel(available) {
  return available ? "Available" : "Unavailable";
}

export function parserTestLabel(result) {
  return result.ok ? `${result.parser_used} passed` : `${result.parser_used} failed`;
}

export function ocrTestLabel(result) {
  return result.ok ? "OCR health test passed" : "OCR health test failed";
}
