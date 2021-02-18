import { Apy, yearn } from "@yfi/sdk";

export type PartialVaults = yearn.vault.Vault & {
  endorsed?: boolean;
};

export type Vault = PartialVaults & {
  apy: Apy | null;
};

export type CachedToken = WithAssets<yearn.vault.Token>;

export type CachedVault = WithAssets<Vault> & {
  token: CachedToken;
  updated: number;
  tvl: number | null;
};

export type WithAssets<T> = T & {
  displayName: string;
  icon: string | null;
};
