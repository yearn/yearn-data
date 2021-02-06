import wrap from "../../utils/wrap";
import { scan } from "../../utils/ddb";

const VAULTS_CACHE = process.env.DDB_VAULTS_CACHE!;

export const handler = wrap(async () => {
  const cached = await scan(VAULTS_CACHE);
  return cached;
});
