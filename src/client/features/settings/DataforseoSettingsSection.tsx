import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, Loader2, RefreshCw, Trash2 } from "lucide-react";
import {
  getDataforseoSettings,
  saveDataforseoSettingsFn,
  removeDataforseoSettingsFn,
  testDataforseoConnectionFn,
  checkDataforseoApiStatusFn,
  type DataforseoConnectionTestResult,
  type DataforseoApiStatusResult,
} from "@/serverFunctions/dataforseoSettings";
import {
  traceDataforseoConnectionTest,
  traceDataforseoStatusCheck,
  traceSettingsMutation,
} from "@/client/features/tracing/settingsTrace";
import { getStandardErrorMessage } from "@/client/lib/error-messages";
import {
  DataforseoCredentialsForm,
  DataforseoTestAlert,
  DataforseoApiHealthCard,
} from "@/client/features/settings/DataforseoSettingsParts";
import {
  DataforseoStatusCard,
  ProviderCircuitAlert,
} from "@/client/features/settings/ProviderCircuitStatus";
import type { DataforseoSettingsView } from "@/serverFunctions/dataforseoSettings";

interface DataforseoSettingsSectionProps {
  scope?: "organization" | "project";
  projectId?: string;
}

function testConnectionLabel(circuitOpen: boolean): string {
  return circuitOpen ? "Retry now" : "Test Connection";
}

function readCircuit(data?: DataforseoSettingsView) {
  return data?.circuit;
}

// The circuit-breaker toggle pushed this section over the complexity/size
// budgets; the branching is inherent form state, so suppress rather than split.
/* eslint-disable eslint/complexity, eslint/max-lines-per-function */
export function DataforseoSettingsSection({
  scope = "organization",
  projectId,
}: DataforseoSettingsSectionProps) {
  const queryClient = useQueryClient();
  const queryKey = ["dataforseoSettings", scope, projectId ?? null];

  const viewQuery = useQuery({
    queryKey,
    queryFn: () =>
      getDataforseoSettings({
        data: { projectId: projectId || undefined },
      }),
  });

  const [loginInput, setLoginInput] = useState("");
  const [passwordInput, setPasswordInput] = useState("");
  const [enabledInput, setEnabledInput] = useState<boolean | null>(null);
  const [circuitBreakerInput, setCircuitBreakerInput] = useState<
    boolean | null
  >(null);
  const [retriesInput, setRetriesInput] = useState<number | null>(null);
  const [priorityInput, setPriorityInput] = useState<number | null>(null);
  const [testResult, setTestResult] =
    useState<DataforseoConnectionTestResult | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [statusResult, setStatusResult] =
    useState<DataforseoApiStatusResult | null>(null);
  const [statusLastChecked, setStatusLastChecked] = useState<Date | null>(null);

  const data = viewQuery.data;
  const circuit = readCircuit(data);

  useEffect(() => {
    if (data) {
      setEnabledInput(data.override ? data.override.enabled : data.enabled);
      setCircuitBreakerInput(
        data.override
          ? data.override.circuitBreakerEnabled
          : data.circuitBreakerEnabled,
      );
      setRetriesInput(
        data.override ? data.override.maxRetries : data.maxRetries,
      );
      setLoginInput("");
      setPasswordInput("");
      setPriorityInput(data.override?.priority ?? data.priority);
    }
  }, [data]);

  const override = data?.override;
  const isConfigured = data?.configured ?? false;
  const isEnabled = enabledInput ?? data?.enabled ?? true;
  const circuitBreakerEnabled = circuitBreakerInput ?? true;
  const maxRetries = retriesInput ?? 2;

  const isDirty =
    loginInput.trim().length > 0 ||
    passwordInput.length > 0 ||
    (priorityInput !== null &&
      priorityInput !== (override?.priority ?? data?.priority ?? 1)) ||
    (enabledInput !== null &&
      enabledInput !== (override?.enabled ?? data?.enabled ?? true)) ||
    (circuitBreakerInput !== null &&
      circuitBreakerInput !==
        (override?.circuitBreakerEnabled ??
          data?.circuitBreakerEnabled ??
          true)) ||
    (retriesInput !== null &&
      retriesInput !== (override?.maxRetries ?? data?.maxRetries ?? 2));

  const saveMutation = useMutation({
    mutationFn: async () => {
      const patch: {
        login?: string;
        password?: string;
        enabled?: boolean;
        circuitBreakerEnabled?: boolean;
        maxRetries?: number;
        priority?: number;
      } = {};
      if (loginInput.trim()) patch.login = loginInput.trim();
      if (passwordInput) patch.password = passwordInput;
      if (enabledInput !== null) patch.enabled = enabledInput;
      if (circuitBreakerInput !== null)
        patch.circuitBreakerEnabled = circuitBreakerInput;
      if (retriesInput !== null) patch.maxRetries = retriesInput;
      if (priorityInput !== null) patch.priority = priorityInput;
      // Trace is observational: single server call, no credentials in trace —
      // only safe presence flags.
      const hasLoginInput = loginInput.trim().length > 0;
      const hasPasswordInput = passwordInput.length > 0;
      return traceSettingsMutation({
        operation: "settings.dataforseo.settings_save",
        source: scope === "project" ? "Project settings" : "Settings",
        projectId: projectId || undefined,
        endpoint: "settings/dataforseo/save",
        metadata: {
          scope,
          hasLoginInput,
          hasPasswordInput,
          enabledChanged:
            enabledInput !== null &&
            enabledInput !== (override?.enabled ?? data?.enabled ?? true),
        },
        counters: { settingsSaved: 1 },
        call: () =>
          saveDataforseoSettingsFn({
            data: { projectId: projectId || undefined, patch },
          }),
      });
    },
    onSuccess: async () => {
      toast.success("DataForSEO settings saved");
      setLoginInput("");
      setPasswordInput("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey }),
        queryClient.invalidateQueries({ queryKey: ["seoApiKeyStatus"] }),
      ]);
    },
    onError: (err) => {
      toast.error(
        getStandardErrorMessage(err, "Failed to save DataForSEO settings"),
      );
    },
  });

  const removeMutation = useMutation({
    mutationFn: async () =>
      traceSettingsMutation({
        operation: "settings.dataforseo.settings_clear",
        source: scope === "project" ? "Project settings" : "Settings",
        projectId: projectId || undefined,
        endpoint: "settings/dataforseo/clear",
        metadata: { scope },
        counters: { settingsCleared: 1 },
        call: () =>
          removeDataforseoSettingsFn({
            data: { projectId: projectId || undefined },
          }),
      }),
    onSuccess: async () => {
      toast.success(
        "Custom credentials cleared; inheriting default configuration",
      );
      setLoginInput("");
      setPasswordInput("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey }),
        queryClient.invalidateQueries({ queryKey: ["seoApiKeyStatus"] }),
      ]);
    },
    onError: (err) => {
      toast.error(getStandardErrorMessage(err, "Failed to clear credentials"));
    },
  });

  const testMutation = useMutation({
    mutationFn: async () =>
      // Global Debug Trace: settings.dataforseo.connection_test.
      // Provider DataForSEO, Billing Free, Metered NO, no SEO cache,
      // no rank check, no budget usage. Credentials stay out of trace.
      traceDataforseoConnectionTest({
        source: scope === "project" ? "Project settings" : "Settings",
        projectId: projectId || undefined,
        scope,
        call: () =>
          testDataforseoConnectionFn({
            data: {
              projectId: projectId || undefined,
              login: loginInput.trim() ? loginInput.trim() : undefined,
              password: passwordInput ? passwordInput : undefined,
            },
          }),
      }),
    onSuccess: async (res) => {
      setTestResult(res);
      setLastChecked(new Date());
      if (res.ok) {
        toast.success(
          res.balance !== null && res.balance !== undefined
            ? `Connected (Balance: $${res.balance.toFixed(2)})`
            : "Connected to DataForSEO",
        );
      } else if (res.reason === "CREDITS_UNAVAILABLE") {
        toast.warning("DataForSEO credits unavailable (HTTP 402)");
      } else if (res.reason === "INVALID_CREDENTIALS") {
        toast.error("DataForSEO authentication failed");
      } else {
        toast.error(`Connection failed: ${res.reason}`);
      }
      await queryClient.invalidateQueries({ queryKey });
    },
    onError: (err) => {
      toast.error(getStandardErrorMessage(err, "Failed to test connection"));
    },
  });

  const checkStatusMutation = useMutation({
    mutationFn: async () =>
      traceDataforseoStatusCheck({
        source: scope === "project" ? "Project settings" : "Settings",
        projectId: projectId || undefined,
        scope,
        call: () =>
          checkDataforseoApiStatusFn({
            data: {
              projectId: projectId || undefined,
              login: loginInput.trim() ? loginInput.trim() : undefined,
              password: passwordInput ? passwordInput : undefined,
            },
          }),
      }),
    onSuccess: (res) => {
      setStatusResult(res);
      setStatusLastChecked(new Date());
      if (res.ok) {
        const operationalCount = res.endpoints.filter(
          (e) => e.status === "ok",
        ).length;
        toast.success(
          `DataForSEO API status checked (${operationalCount}/${res.endpoints.length} operational)`,
        );
      } else if (res.reason === "INVALID_CREDENTIALS") {
        toast.error("DataForSEO authentication failed");
      } else {
        toast.error(`Status check failed: ${res.reason}`);
      }
    },
    onError: (err) => {
      toast.error(getStandardErrorMessage(err, "Failed to check API status"));
    },
  });

  if (viewQuery.isPending) {
    return (
      <div className="flex items-center gap-2 py-4">
        <span className="loading loading-spinner loading-sm" />
        <span className="text-sm text-base-content/50">
          Loading DataForSEO settings…
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-box border border-base-300 bg-base-100 p-5 space-y-6">
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold">DataForSEO</h3>
            <span className="badge badge-sm badge-outline text-base-content/70">
              Paid SEO data provider
            </span>
          </div>
          {override && (
            <span className="badge badge-sm badge-info badge-outline">
              {scope === "project"
                ? "Project override"
                : "Organization override"}
            </span>
          )}
        </div>
        <p className="text-xs text-base-content/60 leading-relaxed">
          DataForSEO provides paid SEO data used for keyword metrics, keyword
          research and SERP data. OpenSEO will continue using available free and
          cached data when DataForSEO is unavailable.
        </p>
      </div>

      <DataforseoStatusCard
        testResult={testResult}
        isConfigured={isConfigured}
        isEnabled={isEnabled}
        circuitBreakerEnabled={circuitBreakerEnabled}
        source={data?.source}
        lastChecked={lastChecked}
        circuit={circuit}
      />

      <ProviderCircuitAlert
        circuit={circuit}
        circuitBreakerEnabled={circuitBreakerEnabled}
      />

      {testResult && <DataforseoTestAlert result={testResult} />}

      <DataforseoApiHealthCard
        statusResult={statusResult}
        isLoading={checkStatusMutation.isPending}
        lastChecked={statusLastChecked}
        onCheckStatus={() => checkStatusMutation.mutate()}
        disabled={checkStatusMutation.isPending || saveMutation.isPending}
      />

      <DataforseoCredentialsForm
        loginInput={loginInput}
        onLoginChange={setLoginInput}
        passwordInput={passwordInput}
        onPasswordChange={setPasswordInput}
        isEnabled={isEnabled}
        onEnabledChange={setEnabledInput}
        loginMasked={data?.loginMasked}
        passwordConfigured={data?.passwordConfigured}
        priority={priorityInput ?? data?.priority ?? 1}
        onPriorityChange={setPriorityInput}
      />

      <div className="rounded-box border border-base-300 bg-base-100 p-3 space-y-1.5">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <label className="flex items-center gap-2 text-xs font-medium">
            <input
              type="checkbox"
              className="toggle toggle-primary toggle-sm"
              checked={circuitBreakerEnabled}
              onChange={(event) => setCircuitBreakerInput(event.target.checked)}
            />
            Circuit breaker
          </label>
          <label className="flex items-center gap-2 text-xs">
            Retries
            <input
              type="number"
              min={0}
              max={5}
              value={maxRetries}
              onChange={(event) =>
                setRetriesInput(
                  Math.min(5, Math.max(0, Number(event.target.value))),
                )
              }
              className="input input-bordered input-sm w-16"
            />
          </label>
        </div>
        <p className="text-[11px] text-base-content/60 leading-relaxed">
          {circuitBreakerEnabled
            ? "Automatically bypass this provider temporarily after repeated or deterministic provider failures."
            : "Circuit protection disabled. OpenSEO will retry this provider on each eligible request before moving to fallback providers."}
        </p>
        <p className="text-[11px] text-base-content/60 leading-relaxed">
          Additional attempts for temporary request failures. Credential,
          account, quota, and configuration errors skip retries and move
          directly to the next provider.
        </p>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-base-200 pt-4">
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn btn-outline btn-sm gap-1.5"
            onClick={() => testMutation.mutate()}
            disabled={testMutation.isPending || saveMutation.isPending}
          >
            {testMutation.isPending ? (
              <>
                <Loader2 className="size-3.5 animate-spin" />
                Testing…
              </>
            ) : (
              <>
                <RefreshCw className="size-3.5" />
                {testConnectionLabel(
                  circuitBreakerEnabled && circuit?.state === "open",
                )}
              </>
            )}
          </button>

          {override && (
            <button
              type="button"
              className="btn btn-ghost btn-sm text-error gap-1.5"
              onClick={() => removeMutation.mutate()}
              disabled={removeMutation.isPending || saveMutation.isPending}
            >
              <Trash2 className="size-3.5" />
              Clear Credentials
            </button>
          )}
        </div>

        <button
          type="button"
          className="btn btn-primary btn-sm gap-1.5"
          onClick={() => saveMutation.mutate()}
          disabled={!isDirty || saveMutation.isPending}
        >
          {saveMutation.isPending ? (
            <>
              <Loader2 className="size-3.5 animate-spin" />
              Saving…
            </>
          ) : (
            <>
              <Check className="size-3.5" />
              Save Changes
            </>
          )}
        </button>
      </div>
    </div>
  );
}
