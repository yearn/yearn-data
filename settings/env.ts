if (!process.env.ETHERSCAN_API_KEY) {
  console.error("[!] please set $ETHERSCAN_API_KEY");
  process.exit(1);
} else if (!process.env.WEB3_PROVIDER_WSS) {
  console.error("[!] please set $WEB3_PROVIDER_WSS");
  process.exit(1);
} else if (!process.env.DDB_VAULTS_CACHE) {
  console.error("[!] please set $DDB_VAULTS_CACHE");
  process.exit(1);
}

export const EtherscanApiKey = process.env.ETHERSCAN_API_KEY;
export const Web3ProviderWss = process.env.WEB3_PROVIDER_WSS;

export const DDBVaultsCache = process.env.DDB_VAULTS_CACHE;
export const SubGraphUrl = process.env.SUBGRAPH_ENDPOINT || '';