function serializeCsv(columns, rows) {
  const csvValue = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
  return [
    columns.map(([label]) => csvValue(label)).join(","),
    ...rows.map((row) => columns.map(([, getValue]) => csvValue(getValue(row))).join(",")),
  ].join("\r\n");
}

function downloadCsv(filename, columns, rows) {
  const blob = new Blob(["\ufeff", serializeCsv(columns, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}