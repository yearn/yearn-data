// Unix timestamp calculator
export default function unix(): number {
  return Math.floor(Date.now() / 1000);
}
