import { protocols } from "@yfi/sdk";

export type PartialVaults = protocols.yearn.vault.Vault & {
  endorsed?: boolean;
};

export type Vault = PartialVaults & {
  apy: protocols.Apy | null;
};

export type CachedToken = WithAssets<protocols.yearn.vault.Token>;

export type CachedVault = WithAssets<Vault> & {
  token: CachedToken;
  updated: number;
  tvl: number | null;
};

export type WithAssets<T> = T & {
  displayName: string;
  icon: string | null;
};
