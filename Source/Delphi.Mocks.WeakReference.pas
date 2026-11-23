{***************************************************************************}
{                                                                           }
{           Delphi Mocks - Taken from DUnitX Project                        }
{                                                                           }
{           Copyright (C) 2013 Vincent Parrett                              }
{                                                                           }
{           vincent@finalbuilder.com                                        }
{           http://www.finalbuilder.com                                     }
{                                                                           }
{                                                                           }
{***************************************************************************}
{                                                                           }
{  Licensed under the Apache License, Version 2.0 (the "License");          }
{  you may not use this file except in compliance with the License.         }
{  You may obtain a copy of the License at                                  }
{                                                                           }
{      http://www.apache.org/licenses/LICENSE-2.0                           }
{                                                                           }
{  Unless required by applicable law or agreed to in writing, software      }
{  distributed under the License is distributed on an "AS IS" BASIS,        }
{  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. }
{  See the License for the specific language governing permissions and      }
{  limitations under the License.                                           }
{                                                                           }
{***************************************************************************}

unit Delphi.Mocks.WeakReference;


interface

{$I 'Delphi.Mocks.inc'}
uses
  System.Generics.Collections;

type
  /// Implemented by our weak referenced object base class
  IWeakReferenceableObject = interface
    ['{3D7F9CB5-27F2-41BF-8C5F-F6195C578755}']
    procedure AddWeakRef(value : Pointer);
    procedure RemoveWeakRef(value : Pointer);
    function GetRefCount : integer;
  end;

  ///  This is our base class for any object that can have a weak reference to
  ///  it. It implements IInterface so the object can also be used just like
  ///  any normal reference counted objects in Delphi.
  TWeakReferencedObject = class(TObject, IInterface, IWeakReferenceableObject)
  private const
    objDestroyingFlag = Integer($80000000);
  protected
  {$IFNDEF AUTOREFCOUNT}
    FRefCount: Integer;
  {$ENDIF}
    FWeakReferences : TList<Pointer>;
    function QueryInterface(const IID: TGUID; out Obj): HResult; stdcall;
    function _AddRef: Integer; virtual; stdcall;
    function _Release: Integer; virtual;stdcall;
    procedure AddWeakRef(value : Pointer);
    procedure RemoveWeakRef(value : Pointer);
    procedure AddWeakRefLocked(value : Pointer);
    procedure RemoveWeakRefLocked(value : Pointer);
    function TryAddRef : boolean;
    function GetRefCount : integer; inline;
  public
    procedure AfterConstruction; override;
    procedure BeforeDestruction; override;
    {$IFDEF NEXTGEN}[Result: Unsafe]{$ENDIF} class function NewInstance: TObject; override;
  {$IFNDEF AUTOREFCOUNT}
    property RefCount: Integer read GetRefCount;
  {$ENDIF}
  end;

  // This is our generic WeakReference interface
  IWeakReference<T : IInterface> = interface
    function IsAlive : boolean;
    function Data : T;
  end;

  //The actual WeakReference implementation.
  TWeakReference<T: IInterface> = class(TInterfacedObject, IWeakReference<T>)
  private
    FData : Pointer;
  protected
    function IsAlive : boolean;
    function Data : T;
  public
    constructor Create(const data : T);
    destructor Destroy;override;
  end;

//only here to work around compiler limitation.
function SafeMonitorTryEnter(const AObject: TObject): Boolean;

//Used by TWeakReference<T>. Declared here because generic methods cannot call
//routines local to the implementation section.
procedure WeakReferenceAttach(var ASlot : Pointer; const AObject : TObject);
procedure WeakReferenceDetach(var ASlot : Pointer);
//Returns the target with an extra reference held, or nil if it is gone or being
//destroyed. A non-nil result must be passed to WeakReferenceUnpin.
function WeakReferencePin(var ASlot : Pointer) : Pointer;
procedure WeakReferenceUnpin(const ATarget : Pointer);

const
 SWeakReferenceError = 'TWeakReference can only be used with objects derived from TWeakReferencedObject';


implementation

uses
  System.TypInfo,
  System.Classes,
  System.Sysutils,
  System.SyncObjs;

var
  //Guards every weak slot and every FWeakReferences list. A slot is only
  //dereferenced or written while this is held, and a target clears its slots
  //under it in BeforeDestruction, so a non-nil slot seen under the lock points
  //at an object whose memory has not been released yet.
  WeakReferenceLock : TCriticalSection;

procedure WeakReferenceAttach(var ASlot : Pointer; const AObject : TObject);
begin
  WeakReferenceLock.Enter;
  try
    TWeakReferencedObject(AObject).AddWeakRefLocked(@ASlot);
    ASlot := Pointer(AObject);
  finally
    WeakReferenceLock.Leave;
  end;
end;

procedure WeakReferenceDetach(var ASlot : Pointer);
begin
  WeakReferenceLock.Enter;
  try
    if ASlot <> nil then
    begin
      TWeakReferencedObject(ASlot).RemoveWeakRefLocked(@ASlot);
      ASlot := nil;
    end;
  finally
    WeakReferenceLock.Leave;
  end;
end;

function WeakReferencePin(var ASlot : Pointer) : Pointer;
begin
  WeakReferenceLock.Enter;
  try
    Result := ASlot;
    if (Result <> nil) and not TWeakReferencedObject(Result).TryAddRef then
      Result := nil;
  finally
    WeakReferenceLock.Leave;
  end;
end;

procedure WeakReferenceUnpin(const ATarget : Pointer);
begin
  if ATarget <> nil then
    TWeakReferencedObject(ATarget)._Release;
end;

//MonitorTryEnter doesn't do a nil check!
function SafeMonitorTryEnter(const AObject: TObject): Boolean;
begin
  if AObject <> nil then
    Result := TMonitor.TryEnter(AObject)
  else
    result := False;
end;


constructor TWeakReference<T>.Create(const data: T);
var
  target : TObject;
begin
  if data = nil then
    raise Exception.Create(format('[%s] passed to TWeakReference was nil', [PTypeInfo(TypeInfo(T)).Name]));

  inherited Create;

  target := IInterface(data) as TObject;
  if not (target is TWeakReferencedObject) then
    raise Exception.Create(SWeakReferenceError);
  WeakReferenceAttach(FData, target);
end;

function TWeakReference<T>.Data: T;
var
  target : Pointer;
begin
  result := Default(T); /// can't assign nil to T
  target := WeakReferencePin(FData);
  if target <> nil then
  try
    //Make sure that the object supports the interface which is our generic type if we
    //simply pass in the interface base type, the method table doesn't work correctly
    if Supports(TObject(target), GetTypeData(TypeInfo(T))^.Guid, result) then
    //if Supports(FData, IInterface, result) then
      result := T(result);
  finally
    WeakReferenceUnpin(target);
  end;
end;

destructor TWeakReference<T>.Destroy;
begin
  WeakReferenceDetach(FData);
  inherited;
end;

function TWeakReference<T>.IsAlive: boolean;
begin
  result := FData <> nil;
end;

{ TWeakReferencedObject }

procedure TWeakReferencedObject.AddWeakRef(value: Pointer);
begin
  WeakReferenceLock.Enter;
  try
    AddWeakRefLocked(value);
  finally
    WeakReferenceLock.Leave;
  end;
end;

procedure TWeakReferencedObject.RemoveWeakRef(value: Pointer);
begin
  WeakReferenceLock.Enter;
  try
    RemoveWeakRefLocked(value);
  finally
    WeakReferenceLock.Leave;
  end;
end;

procedure TWeakReferencedObject.AddWeakRefLocked(value: Pointer);
begin
  if FWeakReferences = nil then
    FWeakReferences := TList<Pointer>.Create;
  FWeakReferences.Add(value);
end;

procedure TWeakReferencedObject.RemoveWeakRefLocked(value: Pointer);
begin
  if FWeakReferences = nil then // should never happen
    {$IFDEF DEBUG}
    raise Exception.Create('FWeakReferences = nil');
    {$ELSE}
    exit;
    {$ENDIF}
  FWeakReferences.Remove(value);
  if FWeakReferences.Count = 0 then
    FreeAndNil(FWeakReferences);
end;

//Takes a reference only while the object is still alive. Once the count has
//reached zero the object is committed to destruction and must not be revived.
function TWeakReferencedObject.TryAddRef: boolean;
var
  LRef : Integer;
begin
  repeat
    LRef := FRefCount;
    if ((LRef and objDestroyingFlag) <> 0) or ((LRef and not objDestroyingFlag) = 0) then
      Exit(False);
  until TInterlocked.CompareExchange(FRefCount, LRef + 1, LRef) = LRef;
  Result := True;
end;

procedure TWeakReferencedObject.AfterConstruction;
begin
{$IFNDEF AUTOREFCOUNT}
  TInterlocked.Decrement(FRefCount);
{$ENDIF}
end;

procedure TWeakReferencedObject.BeforeDestruction;
var
  value : PPointer;
  i: Integer;
begin
{$IFNDEF AUTOREFCOUNT}
  if RefCount <> 0 then
    System.Error(reInvalidPtr);
{$ELSE}
  inherited BeforeDestruction;
{$ENDIF}
  WeakReferenceLock.Enter;
  try
    if FWeakReferences <> nil then
    begin
      for i := 0 to FWeakReferences.Count -1 do
      begin
        value := FWeakReferences.Items[i];
        value^ := nil;
      end;
      FreeAndNil(FWeakReferences);
    end;
  finally
    WeakReferenceLock.Leave;
  end;
end;

function TWeakReferencedObject.GetRefCount: integer;
begin
  Result := FRefCount and not objDestroyingFlag;
end;

class function TWeakReferencedObject.NewInstance: TObject;
begin
  Result := inherited NewInstance;
{$IFNDEF AUTOREFCOUNT}
  // Set an implicit refcount so that refcounting
  // during construction won't destroy the object.
  TWeakReferencedObject(Result).FRefCount := 1;
{$ENDIF}
end;

function TWeakReferencedObject.QueryInterface(const IID: TGUID; out Obj): HResult;
begin
  if GetInterface(IID, Obj) then
    Result := 0
  else
    Result := E_NOINTERFACE;
end;

function TWeakReferencedObject._AddRef: Integer;
begin
{$IFNDEF AUTOREFCOUNT}
  Result := TInterlocked.Increment(FRefCount);
{$ELSE}
  Result := __ObjAddRef;
{$ENDIF}
end;

function TWeakReferencedObject._Release: Integer;

{$IFNDEF AUTOREFCOUNT}
  procedure __MarkDestroying(const Obj);
  var
    LRef: Integer;
  begin
    repeat
      LRef := TWeakReferencedObject(Obj).FRefCount;
    until TInterlocked.CompareExchange(TWeakReferencedObject(Obj).FRefCount, LRef or objDestroyingFlag, LRef) = LRef;
  end;
{$ENDIF}

begin
{$IFNDEF AUTOREFCOUNT}
  Result := TInterlocked.Decrement(FRefCount);
  if Result = 0 then
  begin
    __MarkDestroying(Self);
    Destroy;
  end;
{$ELSE}
  Result := __ObjRelease;
{$ENDIF}
end;

initialization
  WeakReferenceLock := TCriticalSection.Create;

finalization
  FreeAndNil(WeakReferenceLock);

end.
