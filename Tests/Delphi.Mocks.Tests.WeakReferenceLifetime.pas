unit Delphi.Mocks.Tests.WeakReferenceLifetime;

interface

{$M+}
{$IFNDEF FPC}
uses
  DUnitX.TestFramework;
{$ENDIF}

type
  {$IFNDEF FPC}
  [TestFixture]
  {$ENDIF}
  TWeakReferenceLifetimeTests = class
  published
    procedure Destroy_WithoutContention_UnregistersSlot;
    procedure Data_AfterTargetReleased_ReturnsNil;
    procedure VirtualInterfaceWeakRef_DestroyWhileTargetMonitorHeld_UnregistersBeforeFree;
    procedure ChildProxyParentWeakRef_DestroyWhileTargetMonitorHeld_UnregistersBeforeFree;
    procedure TypeInfoProxyParentWeakRef_DestroyWhileTargetMonitorHeld_UnregistersBeforeFree;
  end;
{$M-}

implementation

uses
  SysUtils,
  Classes,
  System.SyncObjs,
  Delphi.Mocks.WeakReference;

type
  IProbe = interface
    ['{B7E0C1D2-4A55-4E18-9C31-6D0A2F8B77C1}']
    procedure Touch;
  end;

  TProbeTarget = class(TWeakReferencedObject, IProbe)
  public
    procedure Touch;
    function WeakCount: Integer;
    function ContainsSlot(Slot: Pointer): Boolean;
    function WeakSlot(Index: Integer): Pointer;
    procedure RedirectAllSlots(NewSlot: Pointer);
  end;

  TWeakReleaser = class(TThread)
  public
    WeakRef: IWeakReference<IProbe>;
    Entered: Integer;
    Finished: Integer;
    constructor Create(const AWeak: IWeakReference<IProbe>);
    procedure Execute; override;
    function WaitForFlag(var Flag: Integer; TimeoutMs: Cardinal): Boolean;
  end;

procedure Check(Condition: Boolean; const Message: string);
begin
  if Condition then
    Exit;
  {$IFNDEF FPC}
  Assert.IsTrue(False, Message);
  {$ELSE}
  raise Exception.Create(Message);
  {$ENDIF}
end;

procedure TProbeTarget.Touch;
begin
end;

function TProbeTarget.WeakCount: Integer;
begin
  if FWeakReferences = nil then
    Result := 0
  else
    Result := FWeakReferences.Count;
end;

function TProbeTarget.ContainsSlot(Slot: Pointer): Boolean;
var
  Index: Integer;
begin
  Result := False;
  if FWeakReferences = nil then
    Exit;
  for Index := 0 to FWeakReferences.Count - 1 do
    if FWeakReferences.Items[Index] = Slot then
      Exit(True);
end;

function TProbeTarget.WeakSlot(Index: Integer): Pointer;
begin
  Result := FWeakReferences.Items[Index];
end;

procedure TProbeTarget.RedirectAllSlots(NewSlot: Pointer);
var
  Index: Integer;
begin
  for Index := 0 to FWeakReferences.Count - 1 do
    FWeakReferences.Items[Index] := NewSlot;
end;

constructor TWeakReleaser.Create(const AWeak: IWeakReference<IProbe>);
begin
  inherited Create(True);
  WeakRef := AWeak;
  FreeOnTerminate := False;
end;

procedure TWeakReleaser.Execute;
begin
  TInterlocked.Exchange(Entered, 1);
  WeakRef := nil;
  TInterlocked.Exchange(Finished, 1);
end;

function TWeakReleaser.WaitForFlag(var Flag: Integer; TimeoutMs: Cardinal): Boolean;
var
  StartedAt: UInt64;
begin
  StartedAt := GetTickCount64;
  repeat
    if TInterlocked.CompareExchange(Flag, 0, 0) <> 0 then
      Exit(True);
    if GetTickCount64 - StartedAt >= TimeoutMs then
      Exit(False);
    Sleep(1);
  until False;
end;

procedure ReleaseWhileTargetMonitorHeld(const Scenario: string);
var
  Target: TProbeTarget;
  TargetRef: IProbe;
  WeakRef: IWeakReference<IProbe>;
  Releaser: TWeakReleaser;
  Slot: Pointer;
  Canary: Pointer;
  MonitorHeld: Boolean;
  FinishedWhileHeld: Boolean;
  SlotRemained: Boolean;
  ClearWroteNil: Boolean;
begin
  Target := TProbeTarget.Create;
  TargetRef := Target;
  { Same construction TProxy.SetParentProxy and TProxyVirtualInterface.Create use. }
  WeakRef := TWeakReference<IProbe>.Create(TargetRef);
  Check(Target.WeakCount = 1, Scenario + ': weak reference was not registered');
  Slot := Target.WeakSlot(0);
  Canary := Pointer($11);
  Releaser := nil;
  MonitorHeld := False;
  try
    TMonitor.Enter(Target);
    MonitorHeld := True;
    Releaser := TWeakReleaser.Create(WeakRef);
    WeakRef := nil;
    Releaser.Start;
    Check(Releaser.WaitForFlag(Releaser.Entered, 5000), Scenario + ': releaser thread did not start');
    { A Destroy that does not wait for the monitor returns immediately.
      A Destroy that keeps the slot until it can remove it stays inside this window. }
    FinishedWhileHeld := Releaser.WaitForFlag(Releaser.Finished, 400);
    SlotRemained := Target.ContainsSlot(Slot);
    if FinishedWhileHeld and SlotRemained then
    begin
      { The freed weak-reference object is still listed. Point that list entry
        at a live word so the real BeforeDestruction store is observable. }
      Target.RedirectAllSlots(@Canary);
      TMonitor.Exit(Target);
      MonitorHeld := False;
      TargetRef := nil;
      Target := nil;
      ClearWroteNil := Canary = nil;
      Check(False, Scenario + ': TWeakReference.Destroy freed the weak reference while its slot was still registered. BeforeDestruction then stored nil through that slot (wrote=' + BoolToStr(ClearWroteNil, True) + ').');
    end
    else
    begin
      TMonitor.Exit(Target);
      MonitorHeld := False;
      Check(Releaser.WaitForFlag(Releaser.Finished, 5000), Scenario + ': releaser did not finish after the target monitor was released');
      Check(not Target.ContainsSlot(Slot), Scenario + ': weak slot was still registered after the weak reference was destroyed');
      TargetRef := nil;
      Target := nil;
    end;
  finally
    if MonitorHeld then
      TMonitor.Exit(Target);
    if Releaser <> nil then
    begin
      Releaser.WaitFor;
      Releaser.Free;
    end;
  end;
end;

procedure TWeakReferenceLifetimeTests.Destroy_WithoutContention_UnregistersSlot;
var
  Target: TProbeTarget;
  TargetRef: IProbe;
  WeakRef: IWeakReference<IProbe>;
begin
  Target := TProbeTarget.Create;
  TargetRef := Target;
  WeakRef := TWeakReference<IProbe>.Create(TargetRef);
  Check(Target.WeakCount = 1, 'uncontended create did not register the weak slot');
  WeakRef := nil;
  Check(Target.WeakCount = 0, 'uncontended Destroy left the weak slot registered');
  TargetRef := nil;
end;

procedure TWeakReferenceLifetimeTests.Data_AfterTargetReleased_ReturnsNil;
var
  TargetRef: IProbe;
  WeakRef: IWeakReference<IProbe>;
  Alive: IProbe;
begin
  TargetRef := TProbeTarget.Create;
  WeakRef := TWeakReference<IProbe>.Create(TargetRef);
  TargetRef := nil;
  Alive := WeakRef.Data;
  Check(Alive = nil, 'Data returned a target that had already been released on this thread');
end;

procedure TWeakReferenceLifetimeTests.VirtualInterfaceWeakRef_DestroyWhileTargetMonitorHeld_UnregistersBeforeFree;
begin
  { TProxy<T>.TProxyVirtualInterface.Create stores TWeakReference<IProxy<T>>.
    Releasing that interface while the proxy monitor is held is the same
    Destroy path. QueryImplementedInterface / SupportsIInterface read it
    through Data and are not part of this assertion. }
  ReleaseWhileTargetMonitorHeld('virtual interface weak ref');
end;

procedure TWeakReferenceLifetimeTests.ChildProxyParentWeakRef_DestroyWhileTargetMonitorHeld_UnregistersBeforeFree;
begin
  { TProxy<T>.SetParentProxy stores TWeakReference<IProxy> on the child.
    TProxy<T>.QueryImplementedInterface reads it through Data. }
  ReleaseWhileTargetMonitorHeld('child proxy parent weak ref');
end;

procedure TWeakReferenceLifetimeTests.TypeInfoProxyParentWeakRef_DestroyWhileTargetMonitorHeld_UnregistersBeforeFree;
begin
  { Delphi.Mocks.Proxy.TypeInfo.TProxy.SetParentProxy and its virtual
    interface use the same TWeakReference<T> destructor. }
  ReleaseWhileTargetMonitorHeld('typeinfo proxy parent weak ref');
end;

{$IFNDEF FPC}
initialization
  TDUnitX.RegisterTestFixture(TWeakReferenceLifetimeTests);
{$ENDIF}

end.
