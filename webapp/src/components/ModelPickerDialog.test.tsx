import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelPickerDialog } from "./ModelPickerDialog";
import type { ModelsPayload } from "@/lib/modelsApi";

const fetchModelsMock = vi.fn<(sessionId?: string) => Promise<ModelsPayload>>();
const selectModelMock = vi.fn<(provider: string, model?: string, effort?: string, sessionId?: string) => Promise<{ ok: boolean; message?: string; error?: string }>>();

vi.mock("@/lib/modelsApi", () => ({
  fetchModels: (sessionId?: string) => fetchModelsMock(sessionId),
  selectModel: (provider: string, model?: string, effort?: string, sessionId?: string) =>
    selectModelMock(provider, model, effort, sessionId),
}));

const mockPayload: ModelsPayload = {
  current: {
    provider: "google",
    model: "gemini-3.8-flash-high",
    effort: "high",
  },
  providers: [
    {
      id: "google",
      label: "Google Antigravity",
      ready: true,
      status: "logged in",
      models: ["gemini-3.8-flash-high", "gemini-2.5-pro", "gemini-2.5-flash"],
      efforts: ["low", "medium", "high"],
      modelEfforts: {
        "gemini-2.5-pro": ["low", "medium", "high", "xhigh"],
      },
      defaultModel: "gemini-3.8-flash-high",
      defaultEffort: "medium",
    },
    {
      id: "deepseek",
      label: "DeepSeek",
      ready: true,
      status: "logged in",
      models: ["deepseek-chat", "deepseek-reasoner"],
      efforts: ["low", "high", "max"],
      defaultModel: "deepseek-chat",
      defaultEffort: "high",
    },
  ],
};

describe("ModelPickerDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchModelsMock.mockResolvedValue(mockPayload);
    selectModelMock.mockResolvedValue({ ok: true, message: "Switched" });
  });

  it("renders providers and models on open", async () => {
    render(
      <ModelPickerDialog
        open={true}
        onOpenChange={vi.fn()}
        currentModel="gemini-3.8-flash-high"
        currentEffort="high"
      />
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Choose Model" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /Google Antigravity/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /DeepSeek/ })).toBeInTheDocument();
      expect(screen.getByText("gemini-3.8-flash-high")).toBeInTheDocument();
      expect(screen.getByText("gemini-2.5-pro")).toBeInTheDocument();
    });
  });

  it("filters models via search input", async () => {
    const user = userEvent.setup();
    render(
      <ModelPickerDialog
        open={true}
        onOpenChange={vi.fn()}
        currentModel="gemini-3.8-flash-high"
      />
    );

    await waitFor(() => {
      expect(screen.getByText("gemini-2.5-pro")).toBeInTheDocument();
    });

    const searchInput = screen.getByRole("textbox", { name: "Filter models" });
    await user.type(searchInput, "pro");

    expect(screen.getByText("gemini-2.5-pro")).toBeInTheDocument();
    expect(screen.queryByText("gemini-3.8-flash-high")).not.toBeInTheDocument();
  });

  it("switches model on button click", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onSuccess = vi.fn();

    render(
      <ModelPickerDialog
        open={true}
        onOpenChange={onOpenChange}
        sessionId="sess-123"
        currentModel="gemini-3.8-flash-high"
        onSuccess={onSuccess}
      />
    );

    await waitFor(() => {
      expect(screen.getByText("gemini-2.5-pro")).toBeInTheDocument();
    });

    expect(fetchModelsMock).toHaveBeenCalledWith("sess-123");

    // Select gemini-2.5-pro
    await user.click(screen.getByText("gemini-2.5-pro"));

    // Select effort "low"
    await user.click(screen.getByRole("radio", { name: "low" }));

    // Click Switch Model
    await user.click(screen.getByRole("button", { name: "Switch Model" }));

    expect(selectModelMock).toHaveBeenCalledWith("google", "gemini-2.5-pro", "low", "sess-123");
    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith("google", "gemini-2.5-pro", "low");
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it("updates reasoning effort options when selecting a model with per-model efforts", async () => {
    const user = userEvent.setup();
    render(
      <ModelPickerDialog
        open={true}
        onOpenChange={vi.fn()}
        currentModel="gemini-3.8-flash-high"
        currentEffort="high"
      />
    );

    await waitFor(() => {
      expect(screen.getByText("gemini-2.5-pro")).toBeInTheDocument();
    });

    expect(screen.queryByRole("radio", { name: "xhigh" })).not.toBeInTheDocument();

    await user.click(screen.getByText("gemini-2.5-pro"));

    expect(screen.getByRole("radio", { name: "xhigh" })).toBeInTheDocument();
  });
});
