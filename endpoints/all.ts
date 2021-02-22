import { DDBVaultsCache } from "../settings/env";
import { scan } from "../utils/ddb";
import wrap from "../utils/wrap";

export const handler = wrap(async () => {
  const cached = await scan(DDBVaultsCache);
  return cached;
});
