import { scan } from "../utils/ddb";
import wrap from "../utils/wrap";

const VaultsCache = process.env.DDB_VAULTS_CACHE!;

export const handler = wrap(async () => {
  const cached = await scan(VaultsCache);
  return cached;
});
